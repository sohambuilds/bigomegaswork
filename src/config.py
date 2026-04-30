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
    mcq_single_threshold: int
    mcq_multi_threshold: int
    headless: bool


config = Config(
    base_url=os.environ.get("BASE_URL", "https://jee-image.onrender.com"),
    username=os.environ["USERNAME"],
    password=os.environ["PASSWORD"],
    model=os.environ.get("LITELLM_MODEL", "gemini/gemini-2.5-pro"),
    mcq_single_threshold=int(os.environ.get("MCQ_SINGLE_THRESHOLD", "90")),
    mcq_multi_threshold=int(os.environ.get("MCQ_MULTI_THRESHOLD", "100")),
    headless=os.environ.get("HEADLESS", "false").lower() == "true",
)
