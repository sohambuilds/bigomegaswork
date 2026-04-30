import json
import logging
from typing import IO
from playwright.sync_api import Page

from .config import config

log = logging.getLogger("exam_runner")
from .browser import (
    login,
    zoom_out,
    get_question_meta,
    get_question_number,
    get_total_questions,
    take_screenshot_b64,
    click_mcq_option,
    enter_numerical,
    click_next,
    click_submit,
)
from .vlm import query_vlm


def _apply_strategy(page: Page, result: dict, q_type: str = "") -> list[str]:
    """
    Decide which actions to take based on VLM result and confidence thresholds.
    Returns a list of action strings for logging.
    """
    q_type = result.get("_type", "")
    actions: list[str] = []

    if "error" in result:
        log.error(f"VLM error for {q_type}: {result['error']}")
        return [f"skip:vlm_error({result['error'][:80]})"]

    if q_type == "mcq-single":
        answer = result.get("answer", "")
        confidence = result.get("confidence", 0)
        if answer in ("A", "B", "C", "D") and confidence >= config.mcq_single_threshold:
            click_mcq_option(page, answer)
            actions.append(f"selected:{answer}(conf={confidence})")
        else:
            actions.append(f"skip:confidence_too_low(conf={confidence})")

    elif q_type == "mcq-multiple":
        confidences = result.get("confidences", {})
        selected = []
        for letter in ("A", "B", "C", "D"):
            conf = confidences.get(letter, 0)
            if conf >= config.mcq_multi_threshold:
                click_mcq_option(page, letter)
                selected.append(f"{letter}(conf={conf})")
        actions.append(f"selected:[{','.join(selected)}]" if selected else "skip:no_option_met_threshold")

    elif q_type == "numerical":
        answer = str(result.get("answer", "")).strip()
        if answer.startswith("-"):
            actions.append(f"skip:negative_answer_unsupported(value={answer})")
        elif answer:
            enter_numerical(page, answer)
            actions.append(f"entered:{answer}(conf={result.get('confidence', '?')})")
        else:
            actions.append("skip:empty_answer")

    return actions if actions else ["skip:unknown"]


def run_paper(page: Page, paper_label: str, log_file: IO) -> dict:
    """
    Run a single paper end-to-end. Returns a summary dict.
    """
    print(f"\n{'='*60}")
    print(f"Starting: {paper_label}")
    print(f"{'='*60}")

    login(page, paper_label)
    zoom_out(page)

    total = get_total_questions(page)
    answered = 0
    skipped = 0

    for q_index in range(total):
        q_num = get_question_number(page)
        q_type, subject = get_question_meta(page)

        print(f"  Q{q_num:>2}/{total} [{q_type:15s}] [{subject:10s}] ", end="", flush=True)

        screenshot = take_screenshot_b64(page)
        vlm_result = query_vlm(screenshot, q_type)
        log.debug(f"Q{q_num}: type={q_type!r} subject={subject!r} vlm={vlm_result}")
        actions = _apply_strategy(page, vlm_result, q_type)

        action_str = " | ".join(actions)
        print(action_str)

        log_entry = {
            "paper": paper_label,
            "q_num": q_num,
            "q_type": q_type,
            "subject": subject,
            "vlm_result": vlm_result,
            "actions": actions,
        }
        log_file.write(json.dumps(log_entry) + "\n")
        log_file.flush()

        if any(a.startswith("skip") for a in actions):
            skipped += 1
        else:
            answered += 1

        if q_num < total:
            click_next(page)

    click_submit(page)
    print(f"\nSubmitted: {paper_label} — answered={answered}, skipped={skipped}")

    return {"paper": paper_label, "answered": answered, "skipped": skipped, "total": total}
