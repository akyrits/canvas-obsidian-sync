import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Copy .env.example to .env and fill it in."
        )
    return value


CANVAS_ICS_URL = _require("CANVAS_ICS_URL")
VAULT_PATH = Path(_require("VAULT_PATH"))
BOARD_PATH = VAULT_PATH / os.environ.get("BOARD_RELATIVE_PATH", "Boards/Assignments.md")
ASSIGNMENTS_ROOT = VAULT_PATH / os.environ.get("ASSIGNMENTS_RELATIVE_DIR", "School")
DUE_SOON_DAYS = int(os.environ.get("DUE_SOON_DAYS", "7"))
