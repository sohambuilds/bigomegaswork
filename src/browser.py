import base64
import logging
import re
import time
from playwright.sync_api import Page

from .config import config

log = logging.getLogger("browser")

ACTION_DELAY = 2.0  # seconds — pause between browser actions for reliability


def _pause(label: str = "") -> None:
    if label:
        log.debug(f"sleep {ACTION_DELAY}s ({label})")
    time.sleep(ACTION_DELAY)


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
    Targets the row CONTAINER (not the bare letter), so for multi-correct
    questions the underlying checkbox actually toggles.
    """
    log.debug(f"Attempting to click option {letter!r}")

    strategies = [
        # The exam UI renders each visible answer as a row like:
        # <div class="mcq-option" data-option="A">...</div>
        f".mcq-option[data-option='{letter}']",
        f"[data-option='{letter}']",
        # Row containing a child whose exact text is the letter
        f".mcq-option:has(:text-is('{letter}'))",
        f"label:has(:text-is('{letter}'))",
        # Direct checkbox/radio with value=A/B/C/D
        f"input[type='checkbox'][value='{letter}'], input[type='radio'][value='{letter}']",
        # Option-label fallback
        f".option-label:text-is('{letter}'), [class*='option-label']:text-is('{letter}')",
        # Last resort: bare letter text
        # (kept last because clicking a bare span often doesn't toggle a checkbox)
    ]
    for sel in strategies:
        try:
            loc = page.locator(sel).first
            loc.scroll_into_view_if_needed(timeout=1500)
            loc.click(timeout=2500)
            log.info(f"Clicked option {letter} via selector: {sel}")
            _pause(f"after option {letter} click")
            return
        except Exception as e:
            log.debug(f"Option {letter} selector failed ({sel}): {e}")

    # Final fallback: bare letter text
    try:
        page.get_by_text(letter, exact=True).first.click(timeout=2500)
        log.info(f"Clicked option {letter} via bare-text fallback")
        _pause(f"after option {letter} click")
    except Exception as e:
        log.error(f"All click strategies failed for option {letter!r}: {e}")


def enter_numerical(page: Page, value_str: str) -> None:
    """Click the numerical answer box, type digits via numpad modal, confirm."""
    log.debug(f"Entering numerical answer: {value_str!r}")

    # The trigger is a small button/input with placeholder "Text..." rendered inline
    # with the question. It is NOT a child of #answer-section. The numpad modal exists
    # in the DOM hidden until the trigger is activated.
    candidates = [
        "input[placeholder^='Text']",
        "[placeholder^='Text']",
        "button:has-text('Text..')",
        "button:has-text('Text...')",
        "[class*='numpad-trigger']",
        "[class*='numpad-input']",
        "[class*='answer-input']",
        "[class*='numerical-input']",
        "input[readonly]:visible",
    ]
    clicked = False
    for sel in candidates:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=1500)
            loc.click(timeout=2500)
            log.debug(f"Numpad trigger clicked via selector: {sel}")
            clicked = True
            break
        except Exception as e:
            log.debug(f"Numpad trigger selector failed ({sel}): {e}")

    if not clicked:
        # Programmatic search: any visible element on the page whose placeholder or
        # text content is exactly/contains "Text..." or which sits next to OK numpad.
        handle = page.evaluate_handle(
            """() => {
                const els = Array.from(document.querySelectorAll('*'));
                const visible = el => {
                    const r = el.getBoundingClientRect();
                    const s = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
                };
                // 1) input/button with Text... placeholder
                let cand = els.find(el => visible(el) && /^Text\.{0,3}$/.test(el.placeholder || ''));
                if (cand) return cand;
                // 2) any small visible element whose text is "Text.", "Text..", or "Text..."
                cand = els.find(el => visible(el) && el.children.length === 0 && /^Text\.{0,3}$/.test((el.textContent || '').trim()));
                if (cand) return cand;
                // 3) any element with class hinting at numpad trigger
                cand = els.find(el => visible(el) && /numpad|num-input|answer-input|numerical/i.test(el.className || ''));
                return cand || null;
            }"""
        )
        try:
            element = handle.as_element()
            if element:
                element.click()
                log.debug("Numpad trigger clicked via JS-located element")
                clicked = True
        except Exception as e:
            log.debug(f"JS-located trigger click failed: {e}")

    if not clicked:
        # Final diagnostic: dump the question-area HTML so we can refine selectors
        try:
            html = page.locator("body").inner_html()
            snippet = html[:4000]
            log.error(f"Numerical trigger not found. body HTML (first 4000 chars):\n{snippet}")
        except Exception:
            pass
        raise RuntimeError("Could not locate numerical answer trigger")

    _pause("after numpad trigger click")
    # Wait for the OK numpad button to actually become visible (not just present in DOM).
    page.wait_for_selector("button.numpad-btn[data-value='ok']:visible, button[data-value='ok']:visible", timeout=8000)
    log.debug("Numpad modal opened")

    # Numpad buttons all carry data-value attributes (e.g. data-value='1', 'ok', 'clear', 'dot').
    def _press(value: str) -> None:
        sel = f"button.numpad-btn[data-value='{value}']:visible, button[data-value='{value}']:visible"
        page.locator(sel).first.click()
        log.debug(f"  Numpad pressed: data-value={value!r}")
        _pause(f"after numpad {value}")

    # Clear any existing value first
    try:
        clear_loc = page.locator("button[data-value='clear']:visible").first
        if clear_loc.count() > 0 and clear_loc.is_visible():
            clear_loc.click()
            log.debug("Cleared existing numpad value")
            _pause("after clear")
    except Exception:
        pass

    for char in value_str:
        if char.isdigit():
            _press(char)
        elif char == ".":
            # Dot button may use data-value='.' or 'dot'
            try:
                _press(".")
            except Exception:
                _press("dot")

    _press("ok")
    # OK closes the numpad
    page.wait_for_selector("button[data-value='ok']:visible", state="hidden", timeout=5000)
    log.info(f"Numerical answer submitted: {value_str!r}")


def click_next(page: Page) -> None:
    log.debug("Clicking Next")
    _pause("before Next")
    page.locator("button:has-text('Next')").click()
    page.wait_for_load_state("networkidle")
    _pause("after Next")


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
