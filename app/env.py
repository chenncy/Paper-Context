"""Load only the repository's .env; isolated diagnostics can opt out."""
import os
from pathlib import Path

from dotenv import load_dotenv


def load_project_env() -> None:
    if os.getenv("PAPER_CONTEXT_SKIP_DOTENV") != "1":
        load_dotenv(Path(__file__).resolve().parents[1] / ".env")
