"""
Social media posting — TikTok and Facebook Page.
Credentials loaded from .env (never hardcoded).
"""
import logging
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

TIKTOK_ACCESS_TOKEN: str = os.getenv("TIKTOK_ACCESS_TOKEN", "")
FB_PAGE_ACCESS_TOKEN: str = os.getenv("FB_PAGE_ACCESS_TOKEN", "")
FB_PAGE_ID: str = os.getenv("FB_PAGE_ID", "")


# ── TikTok ─────────────────────────────────────────────────────────────────────

def tiktok_upload_video(
    video_path: Path,
    title: str,
    tags: list[str] | None = None,
) -> dict:
    """
    Upload a video to TikTok using the Content Posting API v2.
    Returns {"ok": True, "post_id": "..."} or {"ok": False, "error": "..."}
    """
    if not TIKTOK_ACCESS_TOKEN:
        return {"ok": False, "error": "TIKTOK_ACCESS_TOKEN not set in .env"}
    if not video_path.exists():
        return {"ok": False, "error": f"Video file not found: {video_path}"}

    try:
        file_size = video_path.stat().st_size
        caption = title
        if tags:
            hashtags = " ".join(f"#{t.replace(' ', '')}" for t in tags[:5])
            caption = f"{title}\n\n{hashtags}"

        headers = {
            "Authorization": f"Bearer {TIKTOK_ACCESS_TOKEN}",
            "Content-Type": "application/json; charset=UTF-8",
        }

        # Step 1 — Initialize upload
        init_payload = {
            "post_info": {
                "title": caption[:2200],
                "privacy_level": "PUBLIC_TO_EVERYONE",
                "disable_duet": False,
                "disable_comment": False,
                "disable_stitch": False,
            },
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": file_size,
                "chunk_size": file_size,
                "total_chunk_count": 1,
            },
        }
        init_resp = requests.post(
            "https://open.tiktokapis.com/v2/post/publish/video/init/",
            json=init_payload,
            headers=headers,
            timeout=30,
        )
        init_data = init_resp.json()
        if init_data.get("error", {}).get("code") not in ("ok", None, ""):
            err = init_data.get("error", {})
            return {"ok": False, "error": f"TikTok init error: {err.get('message', init_data)}"}

        publish_id  = init_data["data"]["publish_id"]
        upload_url  = init_data["data"]["upload_url"]

        # Step 2 — Upload video bytes
        with open(video_path, "rb") as f:
            video_bytes = f.read()

        upload_headers = {
            "Content-Range": f"bytes 0-{file_size - 1}/{file_size}",
            "Content-Type": "video/mp4",
            "Content-Length": str(file_size),
        }
        upload_resp = requests.put(
            upload_url,
            data=video_bytes,
            headers=upload_headers,
            timeout=300,
        )
        if upload_resp.status_code not in (200, 201, 204):
            return {"ok": False, "error": f"TikTok upload failed: HTTP {upload_resp.status_code}"}

        # Step 3 — Poll status (up to 60s)
        status_url = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"
        for _ in range(12):
            time.sleep(5)
            status_resp = requests.post(
                status_url,
                json={"publish_id": publish_id},
                headers=headers,
                timeout=15,
            ).json()
            status = status_resp.get("data", {}).get("status", "")
            if status == "PUBLISH_COMPLETE":
                logger.info(f"TikTok upload complete: {publish_id}")
                return {"ok": True, "publish_id": publish_id}
            if status in ("FAILED", "PUBLISH_FAILED"):
                return {"ok": False, "error": f"TikTok publish failed: {status_resp}"}

        return {"ok": True, "publish_id": publish_id, "note": "Processing — check TikTok in a few minutes"}

    except Exception as exc:
        logger.error(f"TikTok upload error: {exc}")
        return {"ok": False, "error": str(exc)}


def tiktok_check_credentials() -> dict:
    """Verify TikTok token is valid by calling /v2/user/info/"""
    if not TIKTOK_ACCESS_TOKEN:
        return {"connected": False, "error": "TIKTOK_ACCESS_TOKEN not set"}
    try:
        r = requests.get(
            "https://open.tiktokapis.com/v2/user/info/",
            params={"fields": "display_name,avatar_url"},
            headers={"Authorization": f"Bearer {TIKTOK_ACCESS_TOKEN}"},
            timeout=10,
        )
        data = r.json()
        if data.get("error", {}).get("code") in ("ok", None, ""):
            user = data.get("data", {}).get("user", {})
            return {"connected": True, "username": user.get("display_name", ""), "avatar": user.get("avatar_url", "")}
        return {"connected": False, "error": data.get("error", {}).get("message", "Invalid token")}
    except Exception as exc:
        return {"connected": False, "error": str(exc)}


# ── Facebook ────────────────────────────────────────────────────────────────────

def facebook_post_video(
    video_path: Path,
    title: str,
    description: str = "",
    tags: list[str] | None = None,
) -> dict:
    """
    Upload a video to a Facebook Page using Graph API.
    Returns {"ok": True, "video_id": "..."} or {"ok": False, "error": "..."}
    """
    if not FB_PAGE_ACCESS_TOKEN:
        return {"ok": False, "error": "FB_PAGE_ACCESS_TOKEN not set in .env"}
    if not FB_PAGE_ID:
        return {"ok": False, "error": "FB_PAGE_ID not set in .env"}
    if not video_path.exists():
        return {"ok": False, "error": f"Video file not found: {video_path}"}

    try:
        hashtags = ""
        if tags:
            hashtags = "\n\n" + " ".join(f"#{t.replace(' ', '')}" for t in tags[:8])

        full_desc = f"{description or title}{hashtags}"

        with open(video_path, "rb") as f:
            resp = requests.post(
                f"https://graph-video.facebook.com/v19.0/{FB_PAGE_ID}/videos",
                data={
                    "title":       title[:254],
                    "description": full_desc[:9999],
                    "access_token": FB_PAGE_ACCESS_TOKEN,
                },
                files={"source": ("video.mp4", f, "video/mp4")},
                timeout=300,
            )

        data = resp.json()
        if "id" in data:
            logger.info(f"Facebook video posted: {data['id']}")
            return {"ok": True, "video_id": data["id"],
                    "url": f"https://www.facebook.com/{FB_PAGE_ID}/videos/{data['id']}"}
        return {"ok": False, "error": data.get("error", {}).get("message", str(data))}

    except Exception as exc:
        logger.error(f"Facebook post error: {exc}")
        return {"ok": False, "error": str(exc)}


def facebook_check_credentials() -> dict:
    """Verify Facebook page token is valid."""
    if not FB_PAGE_ACCESS_TOKEN or not FB_PAGE_ID:
        return {"connected": False, "error": "FB_PAGE_ACCESS_TOKEN or FB_PAGE_ID not set"}
    try:
        r = requests.get(
            f"https://graph.facebook.com/v19.0/{FB_PAGE_ID}",
            params={"fields": "name,fan_count", "access_token": FB_PAGE_ACCESS_TOKEN},
            timeout=10,
        )
        data = r.json()
        if "error" in data:
            return {"connected": False, "error": data["error"].get("message", "Invalid token")}
        return {"connected": True, "page_name": data.get("name", ""), "fans": data.get("fan_count", 0)}
    except Exception as exc:
        return {"connected": False, "error": str(exc)}
