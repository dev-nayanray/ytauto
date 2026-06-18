"""
Google Drive integration — backup video assets to ytauto-videos folder.

Single file:  upload_to_drive(video_path, title, output_dir)
Full backup:  backup_full_folder(output_dir, keyword) -> folder URL
"""
import logging
import mimetypes
from pathlib import Path

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from config import YOUTUBE_TOKEN_FILE

logger = logging.getLogger(__name__)

_ROOT_FOLDER_NAME = "ytauto-videos"


def _drive_service():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from upload import SCOPES
    creds = Credentials.from_authorized_user_file(str(YOUTUBE_TOKEN_FILE), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("drive", "v3", credentials=creds)


def _get_or_create_folder(svc, name: str, parent_id: str | None = None) -> str:
    """Return folder ID, creating it if it doesn't exist under parent."""
    query = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    if parent_id:
        query += f" and '{parent_id}' in parents"
    resp = svc.files().list(q=query, fields="files(id)", pageSize=1).execute()
    files = resp.get("files", [])
    if files:
        return files[0]["id"]
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        meta["parents"] = [parent_id]
    f = svc.files().create(body=meta, fields="id").execute()
    return f["id"]


def _upload_file(svc, file_path: Path, parent_id: str) -> str:
    """Upload a single file to Drive folder. Returns file ID."""
    mime = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
    media = MediaFileUpload(str(file_path), mimetype=mime, resumable=file_path.stat().st_size > 5_000_000)
    meta  = {"name": file_path.name, "parents": [parent_id]}
    f = svc.files().create(body=meta, media_body=media, fields="id").execute()
    return f["id"]


def upload_to_drive(video_path: Path, title: str, output_dir: Path) -> str:
    """
    Upload video.mp4 only. Legacy interface kept for backwards compatibility.
    Returns the Drive file ID.
    Idempotent — writes drive_id.txt sentinel.
    """
    sentinel = output_dir / "drive_id.txt"
    if sentinel.exists():
        logger.info(f"Drive upload cache hit → {sentinel.read_text().strip()}")
        return sentinel.read_text(encoding="utf-8").strip()

    svc      = _drive_service()
    root_id  = _get_or_create_folder(svc, _ROOT_FOLDER_NAME)
    file_id  = _upload_file(svc, video_path, root_id)
    sentinel.write_text(file_id, encoding="utf-8")
    logger.info(f"Drive upload complete: file ID {file_id}")
    return file_id


def backup_full_folder(output_dir: Path, keyword: str) -> str:
    """
    Upload ALL video assets to Drive:
      ytauto-videos / <keyword> / video.mp4
                                   script.txt
                                   voice.mp3
                                   thumbnail.jpg
                                   seo.json
                                   clips / clip_00.mp4 ...

    Returns the Drive folder URL (https://drive.google.com/drive/folders/<id>).
    Idempotent — sentinel file drive_folder_id.txt prevents re-uploads.
    """
    sentinel = output_dir / "drive_folder_id.txt"
    if sentinel.exists():
        fid = sentinel.read_text(encoding="utf-8").strip()
        url = f"https://drive.google.com/drive/folders/{fid}"
        logger.info(f"Drive folder cache hit → {url}")
        return url

    svc       = _drive_service()
    root_id   = _get_or_create_folder(svc, _ROOT_FOLDER_NAME)
    folder_id = _get_or_create_folder(svc, keyword[:100], parent_id=root_id)

    # Assets to upload (skip if not present)
    assets = ["video.mp4", "script.txt", "voice.mp3", "thumbnail.jpg", "seo.json"]
    for name in assets:
        p = output_dir / name
        if p.exists():
            try:
                _upload_file(svc, p, folder_id)
                logger.info(f"Drive: uploaded {name}")
            except Exception as exc:
                logger.warning(f"Drive: failed to upload {name}: {exc}")

    # Clips subfolder
    clips_dir = output_dir / "clips"
    if clips_dir.exists():
        clips = sorted(clips_dir.glob("*.mp4"))
        if clips:
            clips_folder_id = _get_or_create_folder(svc, "clips", parent_id=folder_id)
            for clip in clips:
                try:
                    _upload_file(svc, clip, clips_folder_id)
                    logger.info(f"Drive: uploaded clip {clip.name}")
                except Exception as exc:
                    logger.warning(f"Drive: clip upload failed {clip.name}: {exc}")

    # Make folder publicly viewable (anyone with link can view)
    try:
        svc.permissions().create(
            fileId=folder_id,
            body={"type": "anyone", "role": "reader"},
        ).execute()
    except Exception:
        pass

    sentinel.write_text(folder_id, encoding="utf-8")
    url = f"https://drive.google.com/drive/folders/{folder_id}"
    logger.info(f"Drive full backup complete → {url}")
    return url
