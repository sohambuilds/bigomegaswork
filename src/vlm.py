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


def _extract_json(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON object found in VLM response: {text!r}")
    return json.loads(match.group())


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
        log.debug(f"VLM raw response: {raw!r}")
        result = _extract_json(raw)
        log.info(f"VLM parsed result: {result}")
        result["_type"] = key
        result["_raw"] = raw
        return result
    except Exception as exc:
        log.error(f"VLM call failed: {exc}")
        return {"error": str(exc), "_type": key}
