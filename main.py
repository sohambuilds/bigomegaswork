import logging
from datetime import datetime
from pathlib import Path
from playwright.sync_api import sync_playwright

from src.config import config
from src.exam_runner import run_paper

PAPERS = config.paper_labels


def _setup_logging(logs_dir: Path, run_ts: str) -> None:
    log_file = logs_dir / f"run_{run_ts}.log"
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("LiteLLM").setLevel(logging.WARNING)
    logging.getLogger("litellm").setLevel(logging.WARNING)


def main() -> None:
    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)
    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = logs_dir / f"run_{run_ts}.jsonl"

    _setup_logging(logs_dir, run_ts)
    log = logging.getLogger("main")

    log.info(f"Model : {config.model}")
    log.info(f"JSONL : {log_path}")
    log.info(f"Papers: {len(PAPERS)}")

    summaries = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=config.headless)
        context = browser.new_context(viewport={"width": 1920, "height": 1080})
        page = context.new_page()

        with open(log_path, "w", encoding="utf-8") as log_file:
            for paper_label in PAPERS:
                summary = run_paper(page, paper_label, log_file)
                summaries.append(summary)

        browser.close()

    log.info("=" * 60)
    log.info("FINAL SUMMARY")
    log.info("=" * 60)
    total_answered = 0
    total_skipped = 0
    for s in summaries:
        pct = round(100 * s["answered"] / s["total"]) if s["total"] else 0
        log.info(f"  {s['paper']:<35} answered={s['answered']:>2}/{s['total']}  ({pct}%)")
        total_answered += s["answered"]
        total_skipped += s["skipped"]

    grand_total = total_answered + total_skipped
    log.info(f"Total answered: {total_answered}/{grand_total}")
    log.info(f"JSONL log     : {log_path}")


if __name__ == "__main__":
    main()
