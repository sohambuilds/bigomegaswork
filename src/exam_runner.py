import json
import logging
import re
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
    capture_question_snapshot,
    click_mcq_option,
    enter_numerical,
    click_next,
    click_submit,
)
from .vlm import query_vlm

OPTIONS = ("A", "B", "C", "D")


def _submit_after_question(paper_label: str, total_questions: int) -> int:
    """Return the question number where this paper should be submitted."""
    if "sat" in config.base_url.lower() or "sat" in config.exam_label.lower():
        return min(total_questions, 27) if total_questions else 27

    normalized = paper_label.lower()
    if "paper 2" in normalized:
        return 47
    if "paper 1" in normalized:
        return 48
    return total_questions


def _normalize_option(value: object) -> str:
    text = str(value).strip().upper()
    if text in OPTIONS:
        return text
    match = re.search(r"\b([ABCD])\b", text)
    return match.group(1) if match else ""


def _dedupe_options(values: list[str]) -> list[str]:
    selected: list[str] = []
    for value in values:
        option = _normalize_option(value)
        if option and option not in selected:
            selected.append(option)
    return selected


def _options_from_sequence(value: object) -> list[str]:
    if isinstance(value, str):
        return _dedupe_options(re.findall(r"[ABCD]", value.upper()))
    if isinstance(value, (list, tuple)):
        return _dedupe_options([str(item) for item in value])
    return []


def _score_to_float(value: object) -> float:
    try:
        return float(str(value).strip().rstrip("%"))
    except (TypeError, ValueError):
        return 0.0


def _ranked_options_from_mapping(value: object) -> list[str]:
    if not isinstance(value, dict):
        return []
    scored = [
        (option, _score_to_float(value.get(option, 0)))
        for option in OPTIONS
        if option in value
    ]
    scored.sort(key=lambda item: item[1], reverse=True)
    return [option for option, _ in scored]


def _best_single_answer(result: dict) -> str:
    answer = _normalize_option(result.get("answer", ""))
    if answer:
        return answer

    answers = _options_from_sequence(result.get("answers", []))
    if answers:
        return answers[0]

    ranking = _options_from_sequence(result.get("ranking", []))
    if ranking:
        return ranking[0]

    for key in ("probabilities", "confidences"):
        ranked = _ranked_options_from_mapping(result.get(key, {}))
        if ranked:
            return ranked[0]

    flat_scores = {option: result.get(option) for option in OPTIONS if option in result}
    ranked = _ranked_options_from_mapping(flat_scores)
    return ranked[0] if ranked else ""


def _best_multiple_answers(result: dict) -> list[str]:
    answers = _options_from_sequence(result.get("answers", []))
    if answers:
        return answers[:2]

    answer = _normalize_option(result.get("answer", ""))
    if answer:
        return [answer]

    ranking = _options_from_sequence(result.get("ranking", []))
    if ranking:
        return ranking[:2]

    for key in ("probabilities", "confidences"):
        ranked = _ranked_options_from_mapping(result.get(key, {}))
        if ranked:
            return ranked[:2]

    flat_scores = {option: result.get(option) for option in OPTIONS if option in result}
    ranked = _ranked_options_from_mapping(flat_scores)
    return ranked[:2]


def _normalize_numerical_answer(value: object) -> str:
    text = str(value).strip().replace(",", "")
    if not text:
        return "0"

    if text.startswith("-"):
        log.warning(f"Negative numerical answer returned; falling back to 0: {text!r}")
        return "0"

    match = re.search(r"\d+(?:\.\d+)?", text)
    return match.group(0) if match else "0"


def _apply_strategy(page: Page, result: dict, q_type: str = "") -> list[str]:
    """
    Decide which actions to take based on the VLM's final answer.
    Returns a list of action strings for logging.
    """
    q_type = result.get("_type", "")
    actions: list[str] = []

    if "error" in result:
        log.error(f"VLM error for {q_type}: {result['error']}")
        return [f"skip:vlm_error({result['error'][:80]})"]

    if q_type == "mcq-single":
        answer = _best_single_answer(result)
        if answer:
            click_mcq_option(page, answer)
            actions.append(f"selected:{answer}")
        else:
            actions.append("skip:no_valid_option")

    elif q_type == "mcq-multiple":
        selected = _best_multiple_answers(result)
        for letter in selected:
            click_mcq_option(page, letter)
        actions.append(f"selected:[{','.join(selected)}]" if selected else "skip:no_valid_option")

    elif q_type == "numerical":
        answer = _normalize_numerical_answer(result.get("answer", ""))
        enter_numerical(page, answer)
        actions.append(f"entered:{answer}")

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
        snapshot_meta: dict = {}
        try:
            q_num = get_question_number(page)
            q_type, subject = get_question_meta(page)

            print(f"  Q{q_num:>2}/{total} [{q_type:15s}] [{subject:10s}] ", end="", flush=True)

            snapshot = capture_question_snapshot(page)
            snapshot_meta = {
                "source_mode": snapshot.source_mode,
                "question_text_len": len(snapshot.question_text),
                "option_count": len(snapshot.option_texts),
                "image_count": snapshot.image_count,
            }
            time.sleep(VLM_DELAY)
            vlm_result = query_vlm(snapshot, q_type)
            loggable = {k: v for k, v in vlm_result.items() if k != "_raw"}
            log.debug(
                "Q%s: type=%r subject=%r snapshot=%s vlm=%s",
                q_num,
                q_type,
                subject,
                snapshot_meta,
                loggable,
            )
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
                "snapshot": snapshot_meta,
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
