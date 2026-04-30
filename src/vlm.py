import ast
import json
import logging
import re
import litellm

from .config import config

log = logging.getLogger("vlm")

_DECISION_PROCESS = (
    "Use an internal three-step decision process before writing JSON: "
    "1) solve the problem from the screenshot, "
    "2) independently verify the proposed answer against the visible options/question, "
    "3) arbitrate to the final highest-scoring answer. "
    "Do not reveal this reasoning. Output only the final JSON object. "
)

_PROMPTS = {
    "mcq-single": (
        "You are solving a JEE Advanced exam question. "
        "Look at this screenshot carefully — the question text and all four options (A, B, C, D) are visible. "
        "This is a SINGLE-CORRECT MCQ or matching-style question: exactly one option is correct. "
        "You MUST choose exactly one option. Never skip, never return blank, never say unknown. "
        "If uncertain, choose the highest-probability option after verification. "
        + _DECISION_PROCESS
        + "Respond ONLY with valid JSON, no other text: "
        '{\"answer\": \"A\", \"ranking\": [\"A\", \"C\", \"B\", \"D\"], '
        '\"probabilities\": {\"A\": 0.62, \"B\": 0.08, \"C\": 0.22, \"D\": 0.08}} '
        "where answer is one of A/B/C/D, ranking contains all four options from most likely to least likely, "
        "and probabilities are relative likelihoods from 0 to 1."
    ),
    "mcq-multiple": (
        "You are solving a JEE Advanced exam question. "
        "Look at this screenshot carefully — the question text and all four options (A, B, C, D) are visible. "
        "This is a MULTIPLE-CORRECT MCQ: one or more options may be correct. "
        "Select the highest-probability correct options. "
        "You MUST return either one or two options only. Never return zero options. Never return three or four options. "
        "Prefer two options only when both are strongly supported by the verified solution; otherwise return only the best option. "
        + _DECISION_PROCESS
        + "Respond ONLY with valid JSON, no other text: "
        '{\"answers\": [\"B\", \"D\"], \"ranking\": [\"B\", \"D\", \"A\", \"C\"], '
        '\"probabilities\": {\"A\": 0.35, \"B\": 0.78, \"C\": 0.12, \"D\": 0.64}} '
        "where answers has one or two option letters, ranking contains all four options from most likely to least likely, "
        "and probabilities are relative likelihoods from 0 to 1."
    ),
    "numerical": (
        "You are solving a JEE Advanced exam question. "
        "Look at this screenshot carefully — the full question is visible. "
        "This is a NUMERICAL answer question: compute the exact numerical value. "
        "You MUST always provide exactly one answer. Never skip, never return blank, never say unknown. "
        "If uncertain, compute the best possible estimate from the screenshot and still return it. "
        "The answer must be a non-negative number (integer or decimal), with no units and no extra text. "
        + _DECISION_PROCESS
        + "Respond ONLY with valid JSON, no other text: "
        '{\"answer\": \"21\"} '
        "where answer is the computed number as a string."
    ),
}


def _normalize_type(question_type: str) -> str:
    t = question_type.lower().replace(" ", "").replace("-", "")
    if "multiple" in t or "multi" in t:
        return "mcq-multiple"
    if "single" in t or "mcq" in t:
        return "mcq-single"
    if "numerical" in t or "integer" in t or "numeric" in t:
        return "numerical"
    return "mcq-single"


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


def _extract_json(text: str) -> dict:
    """
    The model may return markdown, nested JSON, or reasoning text despite the
    prompt. Extract the last parseable dict containing a relevant answer key.
    """
    cleaned = re.sub(r"```(?:json)?\s*", "", text, flags=re.IGNORECASE).replace("```", "")
    candidates: list[dict] = []

    decoder = json.JSONDecoder()
    for index, char in enumerate(cleaned):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(cleaned[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            candidates.append(value)

    stack: list[int] = []
    for index, char in enumerate(cleaned):
        if char == "{":
            stack.append(index)
        elif char == "}" and stack:
            start = stack.pop()
            parsed = _try_parse(cleaned[start : index + 1])
            if parsed:
                candidates.append(parsed)

    # Walk from end → start, pick first parseable dict with a relevant key
    for parsed in reversed(candidates):
        if any(k in parsed for k in _RELEVANT_KEYS):
            return parsed

    # Fallback: any parseable dict (may still be useful)
    if candidates:
        return candidates[-1]

    raise ValueError(f"Could not parse JSON from VLM response: {text!r}")


def query_vlm(screenshot_b64: str, question_type: str) -> dict:
    """
    Send a screenshot to the VLM and return a parsed answer dict.

    Returns for MCQ-single:            {"answer": "A", "ranking": [...], "probabilities": {...}}
    Returns for MCQ-multiple:          {"answers": ["B", "D"], "ranking": [...], "probabilities": {...}}
    Returns for numerical:             {"answer": "21"}
    On failure returns:                {"error": "<message>", "raw": "<raw response>"}
    """
    key = _normalize_type(question_type)
    prompt = _PROMPTS[key]

    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"},
                },
                {"type": "text", "text": prompt},
            ],
        }
    ]

    log.debug(f"Sending {key} question to model {config.model!r}")
    try:
        response = litellm.completion(model=config.model, messages=messages)
        raw = response.choices[0].message.content or ""
        result = _extract_json(raw)
        log.info(f"VLM parsed result: { {k: v for k, v in result.items() if k != '_raw'} }")
        result["_type"] = key
        result["_raw"] = raw
        return result
    except Exception as exc:
        log.error(f"VLM call failed: {exc}")
        return {"error": str(exc), "_type": key}
