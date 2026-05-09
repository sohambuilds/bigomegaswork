import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv(override=True)


@dataclass
class Config:
    base_url: str
    username: str
    password: str
    model: str
    exam_label: str
    headless: bool


config = Config(
    base_url=os.environ.get("BASE_URL", "https://jee-image.onrender.com"),
    username=os.environ["USERNAME"],
    password=os.environ["PASSWORD"],
    model=os.environ.get("LITELLM_MODEL", "gemini/gemini-2.5-pro"),
    exam_label=os.environ.get("EXAM_LABEL", "JEE Advanced"),
    headless=os.environ.get("HEADLESS", "false").lower() == "true",
)
