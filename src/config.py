import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv(override=True)


def _exam_label_from_env() -> str:
    raw_value = os.environ.get("EXAM_LABEL", "").strip()
    if raw_value:
        return raw_value
    base_url = os.environ.get("BASE_URL", "https://jee-image.onrender.com").lower()
    return "SAT" if "sat" in base_url else "JEE Advanced"


def _paper_labels_from_env() -> list[str]:
    raw_value = os.environ.get("PAPERS", "")
    if raw_value.strip():
        return [item.strip() for item in raw_value.split(",") if item.strip()]

    base_url = os.environ.get("BASE_URL", "https://jee-image.onrender.com").lower()
    exam_label = _exam_label_from_env().lower()
    if "sat" in base_url or "sat" in exam_label:
        return ["Practice Test 4"]

    return [
        "2025 – Paper 1 – English",
        "2025 – Paper 2 – English",
        "2025 – Paper 1 – Hindi",
        "2025 – Paper 2 – Hindi",
    ]


@dataclass
class Config:
    base_url: str
    username: str
    password: str
    model: str
    exam_label: str
    paper_labels: list[str]
    headless: bool


config = Config(
    base_url=os.environ.get("BASE_URL", "https://jee-image.onrender.com"),
    username=os.environ["USERNAME"],
    password=os.environ["PASSWORD"],
    model=os.environ.get("LITELLM_MODEL", "gemini/gemini-2.5-pro"),
    exam_label=_exam_label_from_env(),
    paper_labels=_paper_labels_from_env(),
    headless=os.environ.get("HEADLESS", "false").lower() == "true",
)
