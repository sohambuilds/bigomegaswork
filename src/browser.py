import base64
import logging
import re
from playwright.sync_api import Page

from .config import config

log = logging.getLogger("browser")


def login(page: Page, paper_label: str) -> None:
    log.info(f"Navigating to {config.base_url}")
    page.goto(config.base_url)
    page.wait_for_load_state("networkidle")
    log.debug("Filling login form")
    page.fill("input[placeholder='Enter Username']", config.username)
    page.fill("input[placeholder='Enter Password']", config.password)
    log.debug(f"Selecting paper: {paper_label}")
    page.locator(f"text={paper_label}").click()
    page.click("button:has-text('Login')")
    page.wait_for_load_state("networkidle")
    log.info(f"Logged in — paper: {paper_label}")


def zoom_out(page: Page) -> None:
    log.debug("Zooming out (Ctrl+Minus x5)")
    for i in range(5):
        page.keyboard.press("Control+Minus")
        log.debug(f"  Ctrl+Minus press {i+1}/5")


def get_question_meta(page: Page) -> tuple[str, str]:
    """Returns (question_type, subject) from the badge elements."""
    question_type = page.locator("[class*='question-type-badge']").first.text_content() or "Unknown"
    subject = page.locator(".subject-badge").first.text_content() or "Unknown"
    q_type = question_type.strip()
    subj = subject.strip()
    log.debug(f"Question meta: type={q_type!r}  subject={subj!r}")
    return q_type, subj


def get_question_number(page: Page) -> int:
    header_text = page.locator("text=/Question \\d+ of \\d+/").first.text_content()
    match = re.search(r"Question (\d+) of \d+", header_text or "")
    n = int(match.group(1)) if match else -1
    log.debug(f"Question number: {n}  (header text: {header_text!r})")
    return n


def get_total_questions(page: Page) -> int:
    header_text = page.locator("text=/Question \\d+ of \\d+/").first.text_content()
    match = re.search(r"Question \d+ of (\d+)", header_text or "")
    total = int(match.group(1)) if match else 48
    log.info(f"Total questions in paper: {total}")
    return total


def take_screenshot_b64(page: Page) -> str:
    data = page.screenshot(type="png")
    b64 = base64.b64encode(data).decode("utf-8")
    log.debug(f"Screenshot taken ({len(data)} bytes)")
    return b64


def click_mcq_option(page: Page, letter: str) -> None:
    """
    Click the option row for A/B/C/D.
    Uses exact text match on the standalone letter label to avoid hitting
    letter occurrences inside question or option content.
    """
    log.debug(f"Attempting to click option {letter!r}")
    # First try: element whose complete text is exactly the letter
    locator = page.get_by_text(letter, exact=True).first
    try:
        locator.click(timeout=3000)
        log.info(f"Clicked option {letter} via exact-text match")
        return
    except Exception as e:
        log.warning(f"Exact-text click failed for {letter!r}: {e}")

    # Fallback: look for the option label span/div and click its parent row
    try:
        page.locator(f".option-label:text-is('{letter}'), [class*='option']:text-is('{letter}')").first.click(timeout=3000)
        log.info(f"Clicked option {letter} via option-label fallback")
    except Exception as e:
        log.error(f"All click strategies failed for option {letter!r}: {e}")


def enter_numerical(page: Page, value_str: str) -> None:
    """Click the numerical answer box, type digits via numpad modal, confirm."""
    log.debug(f"Entering numerical answer: {value_str!r}")
    page.locator("[class*='answer'], input[readonly], .numpad-trigger, .answer-box").first.click()
    page.wait_for_selector("button:has-text('OK')", timeout=5000)
    log.debug("Numpad modal opened")

    clear_btn = page.locator("button:has-text('Clear')")
    if clear_btn.is_visible():
        clear_btn.click()
        log.debug("Cleared existing numpad value")

    for char in value_str:
        if char == ".":
            page.locator("button:has-text('.')").click()
        elif char.isdigit():
            page.locator(f"button:has-text('{char}')").click()
        log.debug(f"  Numpad pressed: {char!r}")

    page.locator("button:has-text('OK')").click()
    page.wait_for_selector("button:has-text('OK')", state="hidden", timeout=5000)
    log.info(f"Numerical answer submitted: {value_str!r}")


def click_next(page: Page) -> None:
    log.debug("Clicking Next")
    page.locator("button:has-text('Next')").click()
    page.wait_for_load_state("networkidle")


def click_submit(page: Page) -> None:
    log.info("Clicking Submit Exam")
    page.locator("button:has-text('Submit Exam')").click()
    try:
        page.locator("button:has-text('Yes'), button:has-text('Confirm'), button:has-text('OK')").first.click(timeout=3000)
        log.debug("Confirmed submit dialog")
    except Exception:
        pass
    page.wait_for_load_state("networkidle")
    log.info("Exam submitted")
