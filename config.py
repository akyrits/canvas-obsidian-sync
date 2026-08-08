import os
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

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

# Canvas assignments are due in your local time (e.g. 11:59 PM), but stored/compared
# in UTC. Everything the sync writes uses this IANA zone so dates and times display
# correctly and the board/dashboard categorize by the right calendar day.
TIMEZONE = ZoneInfo(os.environ.get("TIMEZONE", "America/New_York"))

# Optional: a Canvas API token (Account > Settings > "+ New Access Token"). When
# set, the sync fetches each assignment's exact due time from the REST API - the
# ICS feed carries only the date. Without it, due times fall back to 23:59 local.
# The token reads your Canvas data; treat it like a password. CANVAS_API_BASE
# defaults to the host of your ICS feed.
CANVAS_API_TOKEN = os.environ.get("CANVAS_API_TOKEN")
_ics_host = urlparse(CANVAS_ICS_URL)
CANVAS_API_BASE = os.environ.get(
    "CANVAS_API_BASE", f"{_ics_host.scheme}://{_ics_host.netloc}"
)
