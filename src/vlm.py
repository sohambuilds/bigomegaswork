import ast
import json
import logging
import re
import litellm

from .config import config

log = logging.getLogger("vlm")

_PROMPTS = {
    "mcq-single": (
        "You are solving a JEE Advanced exam question. "
        "Look at this screenshot carefully — the question text and all four options (A, B, C, D) are visible. "
        "This is a SINGLE-CORRECT MCQ: exactly one option is correct. "
        "Identify which option is correct and state your confidence. "
        "Respond ONLY with valid JSON, no other text: "
        '{\"answer\": \"A\", \"confidence\": 95} '
        "where answer is one of A/B/C/D and confidence is 0–100."
    ),
    "mcq-multiple": (
        "You are solving a JEE Advanced exam question. "
        "Look at this screenshot carefully — the question text and all four options (A, B, C, D) are visible. "
        "This is a MULTIPLE-CORRECT MCQ: one or more options may be correct. "
        "For each option independently, decide whether it is correct and state your confidence. "
        "Respond ONLY with valid JSON, no other text: "
        '{\"confidences\": {\"A\": 80, \"B\": 100, \"C\": 30, \"D\": 95}} '
        "where each value is your 0–100 confidence that option is correct."
    ),
    "numerical": (
        "You are solving a JEE Advanced exam question. "
        "Look at this screenshot carefully — the full question is visible. "
        "This is a NUMERICAL answer question: compute the exact numerical value. "
        "The answer must be a non-negative number (integer or decimal). "
        "Respond ONLY with valid JSON, no other text: "
        '{\"answer\": \"21\", \"confidence\": 90} '
        "where answer is the computed number as a string and confidence is 0–100."
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


_RELEVANT_KEYS = ("answer", "confidence", "confidences")


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
    The model often returns a long chain-of-thought followed by the JSON answer
    at the end. LaTeX in the reasoning contains `{...}` braces, so a greedy
    regex captures garbage. We instead enumerate every non-nested `{...}` block
    and return the LAST one that parses as a dict containing one of our keys.
    """
    cleaned = re.sub(r"```(?:json)?\s*", "", text, flags=re.IGNORECASE).replace("```", "")
    candidates = re.findall(r"\{[^{}]*\}", cleaned, flags=re.DOTALL)

    # Walk from end → start, pick first parseable dict with a relevant key
    for blob in reversed(candidates):
        parsed = _try_parse(blob)
        if parsed and any(k in parsed for k in _RELEVANT_KEYS):
            return parsed

    # Fallback: any parseable dict (may still be useful)
    for blob in reversed(candidates):
        parsed = _try_parse(blob)
        if parsed:
            return parsed

    raise ValueError(f"Could not parse JSON from VLM response: {text!r}")


def query_vlm(screenshot_b64: str, question_type: str) -> dict:
    """
    Send a screenshot to the VLM and return a parsed answer dict.

    Returns for MCQ-single/numerical:  {"answer": "A", "confidence": 95}
    Returns for MCQ-multiple:          {"confidences": {"A": 80, "B": 100, "C": 30, "D": 95}}
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
