"""
Global configuration — loads .env and exposes typed constants.
All modules import from here; never hardcode secrets elsewhere.
"""
import os
import logging
import shutil
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


# ── FFmpeg resolver ────────────────────────────────────────────────────────────
def _find_exe(name: str) -> str:
    """
    Return the absolute path to ffmpeg or ffprobe.
    Checks PATH first, then the WinGet links directory that winget uses on
    Windows 11 when it installs Gyan.FFmpeg.
    """
    found = shutil.which(name)
    if found:
        return found
    if sys.platform == "win32":
        # winget installs shims here; not always on PATH in existing shells
        winget_links = (
            Path(os.environ.get("LOCALAPPDATA", ""))
            / "Microsoft" / "WinGet" / "Links"
        )
        candidate = winget_links / f"{name}.exe"
        if candidate.exists():
            return str(candidate)
    return name  # last resort — let subprocess raise a clear error


FFMPEG_EXE:  str = _find_exe("ffmpeg")
FFPROBE_EXE: str = _find_exe("ffprobe")

# Add the resolved ffmpeg directory to the process PATH so that third-party
# libraries that call "ffmpeg" as a bare command (e.g. openai-whisper) also
# find it, even when the system PATH hasn't been refreshed in the shell.
_ffmpeg_dir = str(Path(FFMPEG_EXE).parent)
if _ffmpeg_dir not in os.environ.get("PATH", "").split(os.pathsep):
    os.environ["PATH"] = _ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")

# ── Directories ────────────────────────────────────────────────────────────────
BASE_DIR: Path = Path(__file__).parent
OUTPUT_DIR: Path = BASE_DIR / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

# ── API credentials ────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")
PEXELS_API_KEY: str = os.environ.get("PEXELS_API_KEY", "")
YOUTUBE_API_KEY: str = os.environ.get("YOUTUBE_API_KEY", "")   # Data API key (research)
YOUTUBE_CLIENT_SECRETS: Path = BASE_DIR / os.environ.get(
    "YOUTUBE_CLIENT_SECRETS_FILE", "client_secrets.json"
)
YOUTUBE_TOKEN_FILE: Path = BASE_DIR / "token.json"

# ── Channel identity ───────────────────────────────────────────────────────────
CHANNEL_NICHE: str = "Tech, AI, and How-to tutorials"

# ── Claude ─────────────────────────────────────────────────────────────────────
CLAUDE_MODEL: str = "claude-sonnet-4-6"

# ── TTS ────────────────────────────────────────────────────────────────────────
TTS_VOICE: str = os.environ.get("TTS_VOICE", "en-US-GuyNeural")

# ── Video dimensions ───────────────────────────────────────────────────────────
VIDEO_WIDTH: int = 1920
VIDEO_HEIGHT: int = 1080
VIDEO_FPS: int = 30
CLIPS_PER_KEYWORD: int = 12

# ── Script pacing ──────────────────────────────────────────────────────────────
TARGET_VIDEO_MINUTES: int = 10
WORDS_PER_MINUTE: int = 150
TARGET_WORD_COUNT: int = WORDS_PER_MINUTE * TARGET_VIDEO_MINUTES  # ≈ 1 050

# ── YouTube upload limits ──────────────────────────────────────────────────────
MAX_UPLOADS_PER_DAY: int = 6
YOUTUBE_CATEGORY_ID: str = "28"   # Science & Technology
YOUTUBE_PRIVACY: str = os.environ.get("YOUTUBE_PRIVACY", "public")  # "public" or "private"

# ── Telegram Bot ───────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID:   str = os.environ.get("TELEGRAM_CHAT_ID", "")

# ── Google Sheets ──────────────────────────────────────────────────────────────
GOOGLE_SHEET_ID: str = os.environ.get("GOOGLE_SHEET_ID", "")

# ── Logging ────────────────────────────────────────────────────────────────────
LOG_FILE: Path = BASE_DIR / "log.txt"


def setup_logging() -> None:
    """Configure root logger to write to both console and log.txt."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(str(LOG_FILE), encoding="utf-8"),
        ],
    )
    # Suppress verbose googleapiclient cache warning (harmless, just noisy)
    logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)
