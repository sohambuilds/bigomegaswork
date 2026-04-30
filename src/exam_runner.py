import json
import logging
import time
from typing import IO
from playwright.sync_api import Page

from .config import config

VLM_DELAY = 2.0  # seconds — pause before each VLM call

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


def _submit_after_question(paper_label: str, total_questions: int) -> int:
    """Return the question number where this paper should be submitted."""
    normalized = paper_label.lower()
    if "paper 2" in normalized:
        return 47
    if "paper 1" in normalized:
        return 48
    return total_questions


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
        confidences = result.get("confidences")
        if not isinstance(confidences, dict):
            confidences = {letter: result.get(letter, 0) for letter in ("A", "B", "C", "D")}
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
    submit_after = _submit_after_question(paper_label, total)
    log.info(f"Submit target for {paper_label}: Q{submit_after}")
    answered = 0
    skipped = 0
    prev_q_num = -1
    stuck_count = 0
    MAX_ITERATIONS = submit_after + 5  # hard upper bound to prevent infinite loops

    for q_index in range(MAX_ITERATIONS):
        # Detect the final question: Next button is no longer visible.
        # (Submit Exam is always visible on every question, so we can't use it.)
        try:
            next_visible = page.locator("button:has-text('Next'):visible").count() > 0
        except Exception:
            next_visible = True
        is_last = not next_visible
        q_num = -1
        q_type = "?"
        subject = "?"
        vlm_result: dict = {}
        actions: list[str] = []
        try:
            q_num = get_question_number(page)
            q_type, subject = get_question_meta(page)

            print(f"  Q{q_num:>2}/{total} [{q_type:15s}] [{subject:10s}] ", end="", flush=True)

            screenshot = take_screenshot_b64(page)
            time.sleep(VLM_DELAY)
            vlm_result = query_vlm(screenshot, q_type)
            loggable = {k: v for k, v in vlm_result.items() if k != "_raw"}
            log.debug(f"Q{q_num}: type={q_type!r} subject={subject!r} vlm={loggable}")
            actions = _apply_strategy(page, vlm_result, q_type)
        except Exception as exc:
            log.exception(f"Unhandled error on Q{q_num} ({q_type}); advancing to next")
            actions = [f"skip:exception({type(exc).__name__}:{str(exc)[:80]})"]
            print(actions[0])

        if actions:
            action_str = " | ".join(actions)
            # Only print if we didn't already (the print above ran on success path)
            if not any(a.startswith("skip:exception") for a in actions):
                print(action_str)

        try:
            log_entry = {
                "paper": paper_label,
                "q_num": q_num,
                "q_type": q_type,
                "subject": subject,
                "vlm_result": vlm_result,
                "actions": actions,
            }
            log_file.write(json.dumps(log_entry, default=str) + "\n")
            log_file.flush()
        except Exception:
            log.exception("Failed to write log entry")

        if any(a.startswith("skip") for a in actions):
            skipped += 1
        else:
            answered += 1

        # Submit at the requested paper-specific question number after answering it.
        if q_num >= submit_after:
            log.info(f"Submit target reached (Q{q_num}); breaking loop to submit")
            break

        # If the question number didn't advance from last iteration, we're stuck.
        if q_num != -1 and q_num == prev_q_num:
            stuck_count += 1
            log.warning(f"Q{q_num} did not advance (stuck_count={stuck_count})")
            if stuck_count >= 2:
                log.error(f"Stuck on Q{q_num} for {stuck_count} iterations — breaking to submit")
                break
        else:
            stuck_count = 0
        prev_q_num = q_num

        # If this is the last question, stop iterating and submit.
        if is_last:
            log.info(f"Last question reached (Q{q_num}); breaking loop to submit")
            break

        # Otherwise advance via Next.
        try:
            click_next(page)
        except Exception:
            log.exception(f"click_next failed after Q{q_num}; continuing anyway")

    try:
        click_submit(page)
    except Exception:
        log.exception("click_submit failed")
    print(f"\nSubmitted: {paper_label} — answered={answered}, skipped={skipped}")

    return {"paper": paper_label, "answered": answered, "skipped": skipped, "total": total}
