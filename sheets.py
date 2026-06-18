"""
Google Sheets integration — syncs video data to a master spreadsheet.

Sheet layout (one row per video, one sheet "Videos"):
  A  Keyword         B  Title           C  Status
  D  YouTube URL     E  Upload Date     F  Views
  G  Subs Gained     H  Watch Hours     I  Impressions
  J  CTR %           K  Avg View Dur    L  Drive Folder URL
  M  Tags            N  Short URL       O  Rank (keyword search position)
  P  Last Updated

The spreadsheet ID is stored in GOOGLE_SHEET_ID (.env).
If empty, a new spreadsheet named "ytauto — Channel Tracker" is created
automatically and the ID is saved back to .env.
"""
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from googleapiclient.discovery import build

from config import BASE_DIR, OUTPUT_DIR, YOUTUBE_TOKEN_FILE

logger = logging.getLogger(__name__)

SHEET_NAME  = "Videos"
HEADER_ROW  = [
    "Keyword", "Title", "Status", "YouTube URL", "Upload Date",
    "Views", "Subs Gained", "Watch Hours", "Impressions", "CTR %",
    "Avg View Duration", "Drive Folder URL", "Tags", "Short URL",
    "Rank (keyword)", "Last Updated",
]


def _get_creds():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from upload import SCOPES
    creds = Credentials.from_authorized_user_file(str(YOUTUBE_TOKEN_FILE), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds


def _sheets_service():
    return build("sheets", "v4", credentials=_get_creds())


def _ensure_spreadsheet(svc) -> str:
    """Return sheet ID from env, or create a new spreadsheet and save ID."""
    sheet_id = os.environ.get("GOOGLE_SHEET_ID", "").strip()
    if sheet_id:
        return sheet_id

    # Create new spreadsheet
    body = {
        "properties": {"title": "ytauto — Channel Tracker"},
        "sheets": [{
            "properties": {"title": SHEET_NAME},
        }],
    }
    resp     = svc.spreadsheets().create(body=body, fields="spreadsheetId").execute()
    sheet_id = resp["spreadsheetId"]
    logger.info(f"Created spreadsheet: https://docs.google.com/spreadsheets/d/{sheet_id}")

    # Add header row
    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{SHEET_NAME}!A1",
        valueInputOption="RAW",
        body={"values": [HEADER_ROW]},
    ).execute()

    # Persist to .env
    env_path = BASE_DIR / ".env"
    if env_path.exists():
        content = env_path.read_text(encoding="utf-8")
        if "GOOGLE_SHEET_ID=" in content:
            lines = []
            for line in content.splitlines():
                if line.startswith("GOOGLE_SHEET_ID="):
                    lines.append(f"GOOGLE_SHEET_ID={sheet_id}")
                else:
                    lines.append(line)
            env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        else:
            with open(env_path, "a", encoding="utf-8") as f:
                f.write(f"\nGOOGLE_SHEET_ID={sheet_id}\n")
    os.environ["GOOGLE_SHEET_ID"] = sheet_id
    return sheet_id


def _find_row(svc, sheet_id: str, keyword: str) -> int | None:
    """Return 1-based row index for keyword, or None if not found."""
    resp = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=f"{SHEET_NAME}!A:A",
    ).execute()
    values = resp.get("values", [])
    for i, row in enumerate(values):
        if row and row[0].lower() == keyword.lower():
            return i + 1  # 1-based
    return None


def sync_video(keyword: str, output_dir: Path,
               youtube_analytics: dict | None = None,
               drive_folder_url: str = "",
               short_url: str = "",
               rank: int | None = None) -> str:
    """
    Upsert a row for *keyword* in the Google Sheet.
    Returns the spreadsheet URL.
    """
    try:
        svc      = _sheets_service()
        sheet_id = _ensure_spreadsheet(svc)
    except Exception as exc:
        logger.warning(f"Sheets service unavailable: {exc}")
        return ""

    # Gather local data
    title     = keyword
    status    = "assembled"
    yt_url    = ""
    upload_dt = ""
    tags_str  = ""
    short_url_val = short_url

    seo_path = output_dir / "seo.json"
    if seo_path.exists():
        try:
            seo    = json.loads(seo_path.read_text(encoding="utf-8"))
            title  = seo.get("title", keyword)
            tags_str = ", ".join(seo.get("tags", []))
        except Exception:
            pass

    uploaded_path = output_dir / "uploaded.txt"
    if uploaded_path.exists():
        video_id  = uploaded_path.read_text(encoding="utf-8").strip()
        yt_url    = f"https://youtube.com/watch?v={video_id}"
        status    = "uploaded"
        publish_p = output_dir / "publish_at.txt"
        upload_dt = publish_p.read_text(encoding="utf-8").strip()[:10] if publish_p.exists() else ""

    short_p = output_dir / "short" / "uploaded.txt"
    if short_p.exists() and not short_url_val:
        sid = short_p.read_text(encoding="utf-8").strip()
        short_url_val = f"https://youtube.com/shorts/{sid}"

    # Analytics data
    ya = youtube_analytics or {}
    views       = str(ya.get("views", ""))
    subs_gained = str(ya.get("subscribersGained", ""))
    watch_hrs   = str(round(ya.get("estimatedMinutesWatched", 0) / 60, 1)) if ya.get("estimatedMinutesWatched") else ""
    impressions = str(ya.get("impressions", ""))
    ctr         = str(round(ya.get("impressionClickThroughRate", 0) * 100, 2)) if ya.get("impressionClickThroughRate") else ""
    avg_dur     = str(ya.get("averageViewDuration", ""))
    rank_str    = str(rank) if rank is not None else ""

    row = [
        keyword, title, status, yt_url, upload_dt,
        views, subs_gained, watch_hrs, impressions, ctr,
        avg_dur, drive_folder_url, tags_str, short_url_val,
        rank_str, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
    ]

    try:
        existing_row = _find_row(svc, sheet_id, keyword)
        if existing_row:
            svc.spreadsheets().values().update(
                spreadsheetId=sheet_id,
                range=f"{SHEET_NAME}!A{existing_row}",
                valueInputOption="USER_ENTERED",
                body={"values": [row]},
            ).execute()
            logger.info(f"Sheets: updated row {existing_row} for '{keyword}'")
        else:
            svc.spreadsheets().values().append(
                spreadsheetId=sheet_id,
                range=f"{SHEET_NAME}!A1",
                valueInputOption="USER_ENTERED",
                insertDataOption="INSERT_ROWS",
                body={"values": [row]},
            ).execute()
            logger.info(f"Sheets: appended row for '{keyword}'")
    except Exception as exc:
        logger.warning(f"Sheets write failed: {exc}")
        return ""

    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}"
    return url


def get_sheet_url() -> str:
    sheet_id = os.environ.get("GOOGLE_SHEET_ID", "").strip()
    if sheet_id:
        return f"https://docs.google.com/spreadsheets/d/{sheet_id}"
    return ""
