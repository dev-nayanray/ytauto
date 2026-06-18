"""
Authenticates with YouTube via OAuth desktop flow and uploads videos.

First run: the browser opens for the Google OAuth consent screen.
Subsequent runs: token.json is refreshed silently.

Upload behaviour:
  • Privacy defaults to "public" so videos get impressions immediately.
    Override with YOUTUBE_PRIVACY=private in .env to schedule manually.
  • The upload uses the resumable protocol (safe for large files / slow links).
  • Skips upload if output_dir/uploaded.txt already exists (idempotent).
"""
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from config import (
    YOUTUBE_CATEGORY_ID,
    YOUTUBE_CLIENT_SECRETS,
    YOUTUBE_TOKEN_FILE,
    YOUTUBE_PRIVACY,
)

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
    "https://www.googleapis.com/auth/drive.file",
]


# ── Auth ───────────────────────────────────────────────────────────────────────

def _check_secrets_type() -> None:
    """Raise a clear error if client_secrets.json is 'web' type instead of 'installed'."""
    import json as _json
    raw = _json.loads(YOUTUBE_CLIENT_SECRETS.read_text(encoding="utf-8"))
    if "web" in raw and "installed" not in raw:
        raise ValueError(
            "client_secrets.json is a 'Web Application' credential.\n"
            "YouTube OAuth from a desktop app requires an 'Desktop App' credential.\n\n"
            "Fix:\n"
            "  1. Go to https://console.cloud.google.com/apis/credentials\n"
            "  2. Click '+ CREATE CREDENTIALS' → 'OAuth client ID'\n"
            "  3. Application type: 'Desktop app'\n"
            "  4. Download the JSON and save it as client_secrets.json\n"
            "     (replacing the current Web Application file)"
        )


def get_youtube_service():
    """Return an authenticated YouTube API service object."""
    creds: Credentials | None = None

    if YOUTUBE_TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(YOUTUBE_TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.info("Refreshing YouTube OAuth token…")
            creds.refresh(Request())
        else:
            if not YOUTUBE_CLIENT_SECRETS.exists():
                raise FileNotFoundError(
                    f"client_secrets.json not found at {YOUTUBE_CLIENT_SECRETS}. "
                    "Download it from Google Cloud Console → APIs & Services → Credentials."
                )
            _check_secrets_type()
            logger.info("Opening browser for YouTube OAuth consent…")
            flow = InstalledAppFlow.from_client_secrets_file(
                str(YOUTUBE_CLIENT_SECRETS), SCOPES
            )
            creds = flow.run_local_server(port=0)

        YOUTUBE_TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
        logger.info(f"Credentials saved → {YOUTUBE_TOKEN_FILE}")

    return build("youtube", "v3", credentials=creds)


# ── Upload ─────────────────────────────────────────────────────────────────────

def upload_video(
    video_path: Path,
    thumbnail_path: Path,
    seo: dict,
    publish_at: datetime,
    output_dir: Path,
) -> str:
    """
    Upload *video_path* to YouTube.

    Returns the YouTube video ID.
    Skips and returns the stored ID if output_dir/uploaded.txt already exists.
    """
    sentinel = output_dir / "uploaded.txt"
    if sentinel.exists():
        video_id = sentinel.read_text(encoding="utf-8").strip()
        logger.info(f"Upload cache hit (video ID: {video_id})")
        return video_id

    youtube = get_youtube_service()

    title: str = seo.get("title", "Untitled Video")
    desc: str = seo.get("description", "")
    tags: list[str] = seo.get("tags", [])
    hashtags: list[str] = seo.get("hashtags", [])

    full_description = desc + "\n\n" + " ".join(hashtags)

    body = {
        "snippet": {
            "title": title,
            "description": full_description,
            "tags": tags,
            "categoryId": YOUTUBE_CATEGORY_ID,
            "defaultLanguage": "en",
        },
        "status": {
            "privacyStatus": YOUTUBE_PRIVACY,
            "selfDeclaredMadeForKids": False,
        },
    }

    logger.info(f"Uploading '{title}'  (public, immediate)…")
    media = MediaFileUpload(
        str(video_path),
        mimetype="video/mp4",
        resumable=True,
        chunksize=256 * 1_024,
    )
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    video_id = _execute_resumable(request)
    logger.info(f"Upload complete — video ID: {video_id}")

    # Set custom thumbnail (requires channel to be verified on YouTube)
    try:
        _set_thumbnail(youtube, video_id, thumbnail_path)
    except Exception as exc:
        logger.warning(f"Thumbnail upload failed (non-fatal): {exc}")

    sentinel.write_text(video_id, encoding="utf-8")
    (output_dir / "publish_at.txt").write_text(publish_at.isoformat(), encoding="utf-8")
    return video_id


def _execute_resumable(request) -> str:
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            pct = int(status.progress() * 100)
            logger.info(f"  Progress: {pct}%")
    return response["id"]


def _set_thumbnail(youtube, video_id: str, thumbnail_path: Path) -> None:
    logger.info("Setting thumbnail…")
    youtube.thumbnails().set(
        videoId=video_id,
        media_body=MediaFileUpload(str(thumbnail_path), mimetype="image/jpeg"),
    ).execute()
    logger.info("Thumbnail set.")


# ── Schedule helpers ───────────────────────────────────────────────────────────

def get_publish_schedule(count: int, start_hours_ahead: int = 2) -> list[datetime]:
    """
    Return *count* UTC datetimes, staggered 2 hours apart, starting
    *start_hours_ahead* hours from now.
    """
    base = datetime.now(timezone.utc) + timedelta(hours=start_hours_ahead)
    return [base + timedelta(hours=i * 2) for i in range(count)]
