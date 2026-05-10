import ast
import json
import logging
import re
from typing import Any
import litellm

from .browser import QuestionSnapshot
from .config import config

log = logging.getLogger("vlm")

SYSTEM_PROMPT = (
    f"You are an expert {config.exam_label} exam solver.\n"
    "Solve carefully and rigorously. Think extensively before committing to an answer.\n"
    "\n"
    "Workflow you MUST follow for every question:\n"
    "1. UNDERSTAND - restate what is asked and list given data, units, and constraints.\n"
    "2. PLAN - choose the principle, formula, or theorem; note units and assumptions.\n"
    "3. SOLVE - derive step by step; show key algebra, chemistry, or logic.\n"
    "4. VERIFY - re-derive critical steps a second way, substitute back, check dimensions, or test a limiting case.\n"
    "5. ELIMINATE - for every option you do not pick, state the specific reason it is wrong.\n"
    "6. RE-VERIFY - check the chosen answer once more against the question statement and derivation.\n"
    "7. FINAL - emit the ===ANSWER=== block in exactly the required JSON shape.\n"
    "\n"
    "Be thorough in the solving sections. Be exact in the final JSON."
)

_PROMPTS = {
    "mcq-single": (
        "TYPE: MCQ Single Correct (exactly ONE of A/B/C/D)\n"
        "\n"
        "Use this EXACT structure:\n"
        "\n"
        "UNDERSTAND: <restate the question; list given data, units, and what is asked>\n"
        "PLAN: <principle/formula/theorem; key assumptions; units>\n"
        "SOLVE:\n"
        "  <step-by-step derivation; show the algebra/chemistry/logic that matters>\n"
        "VERIFY: <confirm the result by a second route, substitution, dimensional check, or limiting case>\n"
        "ELIMINATE: for EACH of the three options you did NOT pick, give the specific concrete reason it is wrong:\n"
        "  - <wrong-letter-1>: <specific failure>\n"
        "  - <wrong-letter-2>: <specific failure>\n"
        "  - <wrong-letter-3>: <specific failure>\n"
        "RE-VERIFY: <re-check the chosen letter against the question statement before finalizing>\n"
        "\n"
        "===ANSWER===\n"
        '{"answer": "<A|B|C|D>"}\n'
        "\n"
        "STRICT: the JSON value MUST be a single uppercase letter A, B, C, or D as a string. "
        "No prose, markdown, or text after the JSON."
    ),
    "mcq-multiple": (
        "TYPE: MCQ Multiple Correct (ONE or MORE of A/B/C/D may be correct)\n"
        "\n"
        "Use this EXACT structure:\n"
        "\n"
        "UNDERSTAND: <restate the question; list given data, units, and what is asked>\n"
        "PLAN: <principle/formula/theorem; key assumptions; units>\n"
        "SOLVE:\n"
        "  <work out the underlying result needed to judge each option>\n"
        "VERIFY: for EACH option independently, evaluate truth and confirm it:\n"
        "  - A: CORRECT/WRONG - <reason + verification>\n"
        "  - B: CORRECT/WRONG - <reason + verification>\n"
        "  - C: CORRECT/WRONG - <reason + verification>\n"
        "  - D: CORRECT/WRONG - <reason + verification>\n"
        "ELIMINATE: for every option marked WRONG above, restate the specific concrete failure.\n"
        "RE-VERIFY: re-check each option you marked CORRECT against the question before finalizing.\n"
        "\n"
        "===ANSWER===\n"
        '{"answer": ["<letter>", "..."]}\n'
        "\n"
        "STRICT: the JSON value MUST be a JSON array of distinct uppercase letters from A, B, C, D, in alphabetical order. "
        "At least one letter. No prose, markdown, or text after the JSON."
    ),
    "numerical": (
        "TYPE: Numerical Answer (a NUMBER: integer or decimal, can be negative if the question permits it)\n"
        "\n"
        "Use this EXACT structure:\n"
        "\n"
        "UNDERSTAND: <restate the question; list given data, units, and what is asked>\n"
        "PLAN: <principle/formula/method; units; expected sign and order of magnitude>\n"
        "SOLVE:\n"
        "  <step-by-step calculation; keep intermediate values; track units>\n"
        "VERIFY: <confirm by a second method, substitution, or dimensional and limiting check>\n"
        "ELIMINATE: <list at least two plausible wrong answers and the specific reason each is incorrect>\n"
        "RE-VERIFY: <final check of number, requested units, and precision; round only at the end>\n"
        "\n"
        "===ANSWER===\n"
        '{"answer": "<number>"}\n'
        "\n"
        "STRICT: the JSON value MUST be a number rendered as a string. No units, symbols, prose, markdown, or text after the JSON."
    ),
    "matching": (
        "TYPE: Matching / List Match (choose ONE option A/B/C/D)\n"
        "Each option gives a complete set of pair-matches.\n"
        "\n"
        "Use this EXACT structure:\n"
        "\n"
        "UNDERSTAND: <restate the two lists; note constraints on the matches>\n"
        "PLAN: <how each item in the left list will be matched; relevant principle>\n"
        "SOLVE:\n"
        "  <determine the correct match for each left-list item independently>\n"
        "VERIFY: <re-check each pairing via a second argument or cross-check>\n"
        "ELIMINATE: for EACH of the three options you did NOT pick, identify the specific pair inside that option that is wrong and why:\n"
        "  - <wrong-letter-1>: <bad pair and reason>\n"
        "  - <wrong-letter-2>: <bad pair and reason>\n"
        "  - <wrong-letter-3>: <bad pair and reason>\n"
        "RE-VERIFY: <re-check that every pair in the chosen option is correct>\n"
        "\n"
        "===ANSWER===\n"
        '{"answer": "<A|B|C|D>"}\n'
        "\n"
        "STRICT: the JSON value MUST be a single uppercase letter A, B, C, or D as a string. "
        "No prose, markdown, or text after the JSON."
    ),
}


def _normalize_type(question_type: str) -> str:
    t = question_type.lower().replace(" ", "").replace("-", "")
    if "match" in t or "list" in t:
        return "matching"
    if "numerical" in t or "integer" in t or "numeric" in t or "studentresponse" in t or "studentproduced" in t:
        return "numerical"
    if "multiplechoice" in t or "mcq" in t or "single" in t:
        return "mcq-single"
    if "multiple" in t or "multi" in t:
        return "mcq-multiple"
    return "mcq-single"


def _runner_type(prompt_key: str) -> str:
    return "mcq-single" if prompt_key == "matching" else prompt_key


_RELEVANT_KEYS = ("answer", "answers", "ranking", "probabilities", "confidences")


def _try_parse(blob: str) -> dict | None:
    try:
        v = json.loads(blob)
        return v if isinstance(v, dict) else None
    except json.JSONDecodeError:
        pass
    try:
        v = ast.literal_eval(blob)
        return v if isinstance(v, dict) else None
    except (ValueError, SyntaxError):
        pass
    try:
        v = json.loads(blob.replace("'", '"'))
        return v if isinstance(v, dict) else None
    except json.JSONDecodeError:
        return None


def _extract_from_section(section: str) -> dict | None:
    candidates: list[dict] = []

    decoder = json.JSONDecoder()
    for index, char in enumerate(section):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(section[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            candidates.append(value)

    stack: list[int] = []
    for index, char in enumerate(section):
        if char == "{":
            stack.append(index)
        elif char == "}" and stack:
            start = stack.pop()
            parsed = _try_parse(section[start : index + 1])
            if parsed:
                candidates.append(parsed)

    for parsed in reversed(candidates):
        if any(k in parsed for k in _RELEVANT_KEYS):
            return parsed

    if candidates:
        return candidates[-1]

    return None


def _extract_json(text: str) -> dict:
    cleaned = re.sub(r"```(?:json)?\s*", "", text, flags=re.IGNORECASE).replace("```", "")
    marker_match = re.search(r"===ANSWER===\s*(.*)", cleaned, flags=re.DOTALL)
    sections = [marker_match.group(1)] if marker_match else []
    sections.append(cleaned)

    for section in sections:
        parsed = _extract_from_section(section)
        if parsed:
            return parsed

    raise ValueError(f"Could not parse JSON from VLM response: {text!r}")


def _options_from_value(value: Any) -> list[str]:
    if isinstance(value, str):
        raw_options = re.findall(r"[ABCD]", value.upper())
    elif isinstance(value, (list, tuple)):
        raw_options = [str(item).strip().upper() for item in value]
    else:
        raw_options = []

    options: list[str] = []
    for option in raw_options:
        if option in {"A", "B", "C", "D"} and option not in options:
            options.append(option)
    return options


def _normalize_result(parsed: dict, prompt_key: str) -> dict:
    if prompt_key == "mcq-multiple":
        answers = _options_from_value(parsed.get("answer", parsed.get("answers", [])))
        if not answers:
            ranking = _options_from_value(parsed.get("ranking", []))
            answers = ranking[:1]
        if not answers:
            raise ValueError(f"No valid multiple-correct answer in parsed response: {parsed!r}")
        return {"answers": sorted(answers)}

    if prompt_key == "numerical":
        value = str(parsed.get("answer", "")).strip().replace(",", "")
        match = re.search(r"-?\d+(?:\.\d+)?", value)
        if not match:
            raise ValueError(f"No valid numerical answer in parsed response: {parsed!r}")
        return {"answer": match.group(0)}

    options = _options_from_value(parsed.get("answer", ""))
    if not options:
        ranking = _options_from_value(parsed.get("ranking", []))
        options = ranking[:1]
    if not options:
        raise ValueError(f"No valid single-correct answer in parsed response: {parsed!r}")
    return {"answer": options[0]}


def _coerce_snapshot(question_input: QuestionSnapshot | str) -> QuestionSnapshot:
    if isinstance(question_input, QuestionSnapshot):
        return question_input
    return QuestionSnapshot(screenshot_b64=question_input)


def _format_question_context(snapshot: QuestionSnapshot) -> str:
    if not snapshot.has_useful_text:
        return (
            "No reliable extracted DOM question text was found. "
            "Solve from the screenshot image."
        )

    parts = [
        "Extracted DOM question text is available. Use it as the primary source for text-rendered content.",
        "Use the screenshot as the authority for diagrams, images, equations, layout, and anything missing or ambiguous in the text.",
    ]
    if snapshot.question_text:
        parts.extend(["", "Question text:", snapshot.question_text])
    if snapshot.option_texts:
        option_lines = [f"{letter}. {text}" for letter, text in sorted(snapshot.option_texts.items())]
        parts.extend(["", "Option text:", *option_lines])
    return "\n".join(parts)


def query_vlm(question_input: QuestionSnapshot | str, question_type: str) -> dict:
    """
    Send a hybrid question snapshot to the VLM and return a normalized answer dict.

    A raw screenshot base64 string is still accepted for the old screenshot-only path.
    """
    snapshot = _coerce_snapshot(question_input)
    prompt_key = _normalize_type(question_type)
    prompt = _PROMPTS[prompt_key]
    question_context = _format_question_context(snapshot)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"Solve this {config.exam_label} exam question.\n\n"
                        f"{prompt}\n\n"
                        f"{question_context}\n\n"
                        "Question screenshot follows:"
                    ),
                },
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{snapshot.screenshot_b64}"},
                },
                {
                    "type": "text",
                    "text": (
                        "Now solve using the exact structured format above. "
                        "Work through all sections in order and end with ===ANSWER=== followed by the JSON in the exact required shape. "
                        "Do not add any text, markdown, or commentary after the JSON."
                    ),
                },
            ],
        }
    ]

    log.debug(f"Sending {prompt_key} question to model {config.model!r}")
    try:
        response = litellm.completion(
            model=config.model,
            messages=messages,
            temperature=0,
            max_tokens=8192,
        )
        raw = response.choices[0].message.content or ""
        result = _normalize_result(_extract_json(raw), prompt_key)
        log.info(f"VLM parsed result: { {k: v for k, v in result.items() if k != '_raw'} }")
        result["_type"] = _runner_type(prompt_key)
        result["_raw"] = raw
        return result
    except Exception as exc:
        log.error(f"VLM call failed: {exc}")
        return {"error": str(exc), "_type": _runner_type(prompt_key)}
