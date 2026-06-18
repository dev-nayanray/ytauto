"""
Google Drive upload — stores assembled video.mp4 in a Drive folder named 'ytauto-videos'.
Reuses the existing YouTube OAuth token (token.json) which must include drive.file scope.
Skips if output_dir/drive_id.txt already exists (idempotent).
"""
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

FOLDER_NAME = "ytauto-videos"


def upload_to_drive(video_path: Path, title: str, output_dir: Path) -> str:
    """Upload video_path to Google Drive. Returns file ID. Idempotent."""
    sentinel = output_dir / "drive_id.txt"
    if sentinel.exists():
        fid = sentinel.read_text(encoding="utf-8").strip()
        logger.info(f"Drive cache hit (file ID: {fid})")
        return fid

    from config import YOUTUBE_TOKEN_FILE
    from upload import SCOPES
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds = Credentials.from_authorized_user_file(str(YOUTUBE_TOKEN_FILE), SCOPES)
    if not creds.valid and creds.expired and creds.refresh_token:
        creds.refresh(Request())

    drive = build("drive", "v3", credentials=creds)

    # Get or create folder
    resp = drive.files().list(
        q=f"name='{FOLDER_NAME}' and mimeType='application/vnd.google-apps.folder' and trashed=false",
        fields="files(id)"
    ).execute()
    files = resp.get("files", [])
    if files:
        folder_id = files[0]["id"]
    else:
        folder = drive.files().create(
            body={"name": FOLDER_NAME, "mimeType": "application/vnd.google-apps.folder"},
            fields="id"
        ).execute()
        folder_id = folder["id"]

    media = MediaFileUpload(str(video_path), mimetype="video/mp4", resumable=True, chunksize=256 * 1024)
    file_meta = {"name": f"{title}.mp4", "parents": [folder_id]}
    logger.info(f"Uploading to Drive: {title}")
    result = drive.files().create(body=file_meta, media_body=media, fields="id").execute()
    file_id = result["id"]
    sentinel.write_text(file_id, encoding="utf-8")
    logger.info(f"Drive upload complete — file ID: {file_id}")
    return file_id
