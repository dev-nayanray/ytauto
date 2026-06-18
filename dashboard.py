"""
Visual dashboard for the ytauto pipeline.

Run:  python dashboard.py
Open: http://127.0.0.1:8000
"""
import asyncio
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Windows requires ProactorEventLoop for subprocess support
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))
from config import OUTPUT_DIR, YOUTUBE_CLIENT_SECRETS, YOUTUBE_TOKEN_FILE, setup_logging

setup_logging()
OUTPUT_DIR.mkdir(exist_ok=True)

# Warn if token.json is missing the new spreadsheets scope (needs re-auth)
def _check_token_scopes() -> None:
    import json as _j
    if not YOUTUBE_TOKEN_FILE.exists():
        return
    try:
        scopes = _j.loads(YOUTUBE_TOKEN_FILE.read_text(encoding="utf-8")).get("scopes", [])
        missing = [s for s in [
            "https://www.googleapis.com/auth/spreadsheets",
        ] if s not in scopes]
        if missing:
            import logging as _log
            _log.getLogger(__name__).warning(
                "token.json is missing scopes: %s — "
                "delete token.json and restart to re-authenticate (opens browser once).",
                missing,
            )
    except Exception:
        pass

_check_token_scopes()

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app_: FastAPI):  # type: ignore[override]
    tasks = [
        asyncio.create_task(_start_telegram_listener()),
        asyncio.create_task(_scheduler_loop()),
        asyncio.create_task(_milestone_checker_loop()),
    ]
    yield
    for t in tasks:
        t.cancel()

app = FastAPI(title="ytauto Dashboard", lifespan=lifespan)
app.mount("/output", StaticFiles(directory=str(OUTPUT_DIR)), name="output")
_tmpl = Jinja2Templates(directory=str(BASE / "templates"))

_ws_clients: list[WebSocket] = []
_running: bool = False
_stop_requested: bool = False
_oauth_running: bool = False
_current_keyword: str = ""
_cached_analytics: dict = {}
_active_tasks: dict[str, dict] = {}   # task_id -> {type, slug, description, started_at}


# ── Active task tracker ────────────────────────────────────────────────────────

import time as _time

async def _task_start(task_id: str, task_type: str, slug: str, description: str) -> None:
    _active_tasks[task_id] = {
        "type": task_type, "slug": slug,
        "description": description,
        "started_at": _time.time(),
    }
    await _broadcast({"type": "tasks_update", "tasks": list(_active_tasks.values())})


async def _task_end(task_id: str) -> None:
    _active_tasks.pop(task_id, None)
    await _broadcast({"type": "tasks_update", "tasks": list(_active_tasks.values())})


# ── Broadcast ──────────────────────────────────────────────────────────────────

async def _broadcast(msg: dict) -> None:
    dead: list[WebSocket] = []
    for ws in list(_ws_clients):
        try:
            await ws.send_json(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in _ws_clients:
            _ws_clients.remove(ws)


# ── Video metadata scanner ─────────────────────────────────────────────────────

def _scan_videos() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if not OUTPUT_DIR.exists():
        return result
    for d in sorted(OUTPUT_DIR.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        clips = len(list((d / "clips").glob("*.mp4"))) if (d / "clips").exists() else 0
        v: dict[str, Any] = {
            "slug": d.name,
            "keyword": d.name.replace("_", " "),
            "stages": {
                "research":  True,
                "script":    (d / "script.txt").exists(),
                "voice":     (d / "voice.mp3").exists(),
                "visuals":   clips >= 1,
                "assemble":  (d / "video.mp4").exists(),
                "seo":       (d / "seo.json").exists(),
                "thumbnail": (d / "thumbnail.jpg").exists(),
                "upload":    (d / "uploaded.txt").exists(),
            },
            "clips_count":   clips,
            "thumbnail_url": f"/output/{d.name}/thumbnail.jpg" if (d / "thumbnail.jpg").exists() else None,
            "title":     None,
            "video_id":  None,
            "size_mb":   None,
        }
        if (d / "seo.json").exists():
            try:
                seo = json.loads((d / "seo.json").read_text(encoding="utf-8"))
                v["title"] = seo.get("title")
            except Exception:
                pass
        if (d / "uploaded.txt").exists():
            v["video_id"] = (d / "uploaded.txt").read_text(encoding="utf-8").strip()
        if (d / "publish_at.txt").exists():
            v["publish_at"] = (d / "publish_at.txt").read_text(encoding="utf-8").strip()
        if (d / "video.mp4").exists():
            v["size_mb"] = round((d / "video.mp4").stat().st_size / 1_048_576, 1)
        # Short metadata
        short_dir = d / "short"
        v["has_short"]       = (short_dir / "video.mp4").exists()
        v["short_video_id"]  = (short_dir / "uploaded.txt").read_text(encoding="utf-8").strip() \
                                if (short_dir / "uploaded.txt").exists() else None
        v["short_script"]    = (short_dir / "script.txt").exists()
        # Upload date from uploaded.txt modification time
        uploaded_file = d / "uploaded.txt"
        if uploaded_file.exists():
            import datetime as _dtt
            v["upload_date"] = _dtt.datetime.fromtimestamp(
                uploaded_file.stat().st_mtime).strftime("%Y-%m-%d")
        else:
            v["upload_date"] = None
        result.append(v)
    return result


# ── HTTP routes ────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return _tmpl.TemplateResponse("index.html", {"request": request})


def _secrets_type() -> str:
    """Return 'installed', 'web', or 'missing'."""
    if not YOUTUBE_CLIENT_SECRETS.exists():
        return "missing"
    try:
        raw = json.loads(YOUTUBE_CLIENT_SECRETS.read_text(encoding="utf-8"))
        if "installed" in raw:
            return "installed"
        if "web" in raw:
            return "web"
    except Exception:
        pass
    return "unknown"


@app.get("/api/status")
async def get_status() -> JSONResponse:
    stype = _secrets_type()
    token_ok = YOUTUBE_TOKEN_FILE.exists()
    secrets_ok = stype == "installed"
    return JSONResponse({
        "running":       _running,
        "videos":        _scan_videos(),
        "token_ok":      token_ok,
        "secrets_ok":    secrets_ok,
        "secrets_type":  stype,
    })


@app.get("/api/youtube/channel")
async def get_channel() -> JSONResponse:
    if not YOUTUBE_TOKEN_FILE.exists():
        return JSONResponse({"connected": False, "reason": "no_token"})
    try:
        info = await asyncio.to_thread(_fetch_channel_info)
        return JSONResponse({"connected": True, **info})
    except Exception as e:
        return JSONResponse({"connected": False, "reason": str(e)})


def _fetch_channel_info() -> dict:
    from upload import get_youtube_service
    yt = get_youtube_service()
    resp = yt.channels().list(part="snippet,statistics", mine=True).execute()
    items = resp.get("items", [])
    if not items:
        return {}
    ch = items[0]
    snippet = ch["snippet"]
    stats = ch.get("statistics", {})
    return {
        "channel_id":       ch["id"],
        "title":            snippet["title"],
        "thumbnail":        (snippet.get("thumbnails", {}).get("high", {}).get("url")
                             or snippet.get("thumbnails", {}).get("default", {}).get("url")),
        "subscriber_count": int(stats.get("subscriberCount", 0)),
        "subscriberCount":  str(stats.get("subscriberCount", 0)),  # camelCase alias for template
        "video_count":      int(stats.get("videoCount", 0)),
        "view_count":       int(stats.get("viewCount", 0)),
    }


@app.post("/api/youtube/reauth")
async def youtube_reauth() -> JSONResponse:
    """Delete token.json and re-run full OAuth to pick up new scopes."""
    if YOUTUBE_TOKEN_FILE.exists():
        YOUTUBE_TOKEN_FILE.unlink()
        logger.info("token.json deleted — re-auth required")
    return await youtube_connect()


@app.post("/api/youtube/connect")
async def youtube_connect() -> JSONResponse:
    global _oauth_running
    if not YOUTUBE_CLIENT_SECRETS.exists():
        return JSONResponse({"error": "client_secrets.json not found"}, status_code=400)
    if _oauth_running:
        return JSONResponse({"error": "OAuth already in progress"}, status_code=409)
    _oauth_running = True
    asyncio.create_task(_oauth_task())
    return JSONResponse({"status": "started"})


async def _oauth_task() -> None:
    global _oauth_running
    try:
        await _broadcast({"type": "log", "level": "INFO",
                          "text": "Opening browser for YouTube OAuth consent..."})
        await asyncio.to_thread(_run_blocking_oauth)
        info = await asyncio.to_thread(_fetch_channel_info)
        await _broadcast({
            "type":     "youtube_connected",
            "success":  True,
            "token_ok": True,
            "channel":  info,
        })
        await _broadcast({"type": "log", "level": "INFO",
                          "text": f"YouTube connected: {info.get('title', 'channel')}"})
    except Exception as exc:
        await _broadcast({"type": "youtube_connected", "success": False, "error": str(exc)})
        await _broadcast({"type": "log", "level": "ERROR",
                          "text": f"YouTube OAuth failed: {exc}"})
    finally:
        _oauth_running = False


def _run_blocking_oauth() -> None:
    from google_auth_oauthlib.flow import InstalledAppFlow
    from upload import SCOPES
    flow = InstalledAppFlow.from_client_secrets_file(str(YOUTUBE_CLIENT_SECRETS), SCOPES)
    creds = flow.run_local_server(port=0)
    YOUTUBE_TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")


@app.post("/api/upload/{slug}")
async def manual_upload(slug: str) -> JSONResponse:
    video_dir = OUTPUT_DIR / slug
    if not video_dir.exists():
        return JSONResponse({"error": "Video not found"}, status_code=404)
    video_path = video_dir / "video.mp4"
    seo_path   = video_dir / "seo.json"
    if not video_path.exists():
        return JSONResponse({"error": "video.mp4 not assembled yet"}, status_code=400)
    if not seo_path.exists():
        return JSONResponse({"error": "seo.json not found — run pipeline first"}, status_code=400)
    if (video_dir / "uploaded.txt").exists():
        vid = (video_dir / "uploaded.txt").read_text(encoding="utf-8").strip()
        return JSONResponse({"status": "already_uploaded", "video_id": vid})
    asyncio.create_task(_manual_upload_task(slug, video_dir))
    return JSONResponse({"status": "upload_started", "slug": slug})


async def _manual_upload_task(slug: str, video_dir: Path) -> None:
    await _task_start(f"upload:{slug}", "upload", slug, f"Uploading: {slug.replace('_',' ')}")
    try:
        label = slug.replace("_", " ")
        await _broadcast({"type": "log", "level": "INFO",
                          "text": f"Manual upload starting: {label}"})
        import json as _json
        from datetime import datetime, timedelta, timezone
        seo = _json.loads((video_dir / "seo.json").read_text(encoding="utf-8"))
        publish_at = datetime.now(timezone.utc) + timedelta(hours=2)
        video_id = await asyncio.to_thread(
            _blocking_upload, video_dir / "video.mp4",
            video_dir / "thumbnail.jpg", seo, publish_at, video_dir,
        )
        await _broadcast({
            "type":     "upload_complete",
            "slug":     slug,
            "video_id": video_id,
            "videos":   _scan_videos(),
        })
        await _broadcast({"type": "log", "level": "INFO",
                          "text": f"Upload complete — video ID: {video_id}"})
    except Exception as exc:
        await _broadcast({"type": "log", "level": "ERROR",
                          "text": f"Manual upload failed for {slug}: {exc}"})
    finally:
        await _task_end(f"upload:{slug}")


def _blocking_upload(
    video_path: Path, thumb_path: Path,
    seo: dict, publish_at: Any, output_dir: Path,
) -> str:
    from upload import upload_video
    return upload_video(video_path, thumb_path, seo, publish_at, output_dir)


@app.get("/api/youtube/analytics")
async def get_youtube_analytics() -> JSONResponse:
    global _cached_analytics
    if not YOUTUBE_TOKEN_FILE.exists():
        return JSONResponse({"connected": False, "reason": "no_token"})
    try:
        data = await asyncio.to_thread(_fetch_full_analytics)
        _cached_analytics = {"connected": True, **data}
        return JSONResponse(_cached_analytics)
    except Exception as exc:
        return JSONResponse({"connected": False, "reason": str(exc)})


def _fetch_full_analytics() -> dict:
    import logging as _log
    from datetime import date, timedelta
    from upload import get_youtube_service, SCOPES
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    yt = get_youtube_service()
    ch_resp = yt.channels().list(part="snippet,statistics", mine=True).execute()
    if not ch_resp.get("items"):
        return {"channel": None, "videos": [], "channel_analytics": {}}
    ch = ch_resp["items"][0]
    snippet = ch["snippet"]
    stats = ch.get("statistics", {})
    channel_data = {
        "id":          ch["id"],
        "title":       snippet.get("title", ""),
        "thumbnail":   (snippet.get("thumbnails", {}).get("high", {}).get("url")
                        or snippet.get("thumbnails", {}).get("default", {}).get("url")),
        "total_views": int(stats.get("viewCount", 0)),
        "subscribers": int(stats.get("subscriberCount", 0)),
        "video_count": int(stats.get("videoCount", 0)),
        "hidden_subs": bool(stats.get("hiddenSubscriberCount", False)),
    }

    # YouTube Analytics API — impressions, CTR, watch time (last 28 days)
    channel_analytics: dict = {}
    try:
        creds = Credentials.from_authorized_user_file(str(YOUTUBE_TOKEN_FILE), SCOPES)
        ya = build("youtubeAnalytics", "v2", credentials=creds)
        end_date   = date.today()
        start_date = end_date - timedelta(days=28)
        ya_resp = ya.reports().query(
            ids="channel==MINE",
            startDate=start_date.isoformat(),
            endDate=end_date.isoformat(),
            metrics="views,estimatedMinutesWatched,averageViewDuration,impressions,impressionClickThroughRate,subscribersGained",
        ).execute()
        if ya_resp.get("rows"):
            headers = [h["name"] for h in ya_resp["columnHeaders"]]
            channel_analytics = dict(zip(headers, ya_resp["rows"][0]))
    except Exception as exc:
        _log.getLogger(__name__).warning(f"YouTube Analytics API: {exc}")

    # Monetization progress tracker
    total_views = channel_data["total_views"]
    subscribers = channel_data["subscribers"]

    # YouTube Partner Program thresholds
    monetization = {
        "subscribers_goal": 1000,
        "subscribers_current": subscribers,
        "subscribers_pct": min(100, round(subscribers / 1000 * 100, 1)),
        "watch_hours_goal": 4000,
        "watch_hours_current": round(channel_analytics.get("estimatedMinutesWatched", 0) / 60, 1),
        "watch_hours_pct": min(100, round(
            (channel_analytics.get("estimatedMinutesWatched", 0) / 60) / 4000 * 100, 2
        )),
        "eligible": subscribers >= 1000,  # simplified check
    }

    video_stats: list[dict] = []
    for d in sorted(OUTPUT_DIR.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        uploaded_file = d / "uploaded.txt"
        if not uploaded_file.exists():
            continue
        video_id = uploaded_file.read_text(encoding="utf-8").strip()
        if not video_id:
            continue
        try:
            v_resp = yt.videos().list(part="statistics,snippet", id=video_id).execute()
            if v_resp.get("items"):
                v = v_resp["items"][0]
                vstats = v.get("statistics", {})
                video_stats.append({
                    "id":        video_id,
                    "slug":      d.name,
                    "title":     v["snippet"]["title"],
                    "published": v["snippet"].get("publishedAt", ""),
                    "thumbnail": v["snippet"].get("thumbnails", {}).get("medium", {}).get("url"),
                    "views":     int(vstats.get("viewCount", 0)),
                    "likes":     int(vstats.get("likeCount", 0)),
                    "comments":  int(vstats.get("commentCount", 0)),
                    "drive_id":  (d / "drive_id.txt").read_text(encoding="utf-8").strip()
                                  if (d / "drive_id.txt").exists() else None,
                })
        except Exception:
            pass
    # Normalised summary used by dashboard + Telegram bot
    watch_hours = round(channel_analytics.get("estimatedMinutesWatched", 0) / 60, 1)
    ctr_raw     = channel_analytics.get("impressionClickThroughRate", 0) or 0
    avg_dur_s   = int(channel_analytics.get("averageViewDuration", 0) or 0)
    avg_dur_str = f"{avg_dur_s // 60}m {avg_dur_s % 60}s" if avg_dur_s else "—"

    summary = {
        "total_views":       channel_analytics.get("views", 0) or channel_data["total_views"],
        "watch_hours":       watch_hours,
        "subscribers":       channel_data["subscribers"],
        "estimated_revenue": round(channel_analytics.get("estimatedRevenue", 0.0) or 0.0, 2),
        "ctr":               round(ctr_raw * 100, 1),
        "avg_view_duration": avg_dur_str,
    }
    ypp = {
        "eligible":         channel_data["subscribers"] >= 1000 and watch_hours >= 4000,
        "watch_hours":      watch_hours,
        "watch_hours_pct":  min(100.0, round(watch_hours / 4000 * 100, 1)),
        "subscribers":      channel_data["subscribers"],
        "subscribers_pct":  min(100.0, round(channel_data["subscribers"] / 1000 * 100, 1)),
    }

    # Add per-video fields the frontend expects
    for v in video_stats:
        slug = v.get("slug", "")
        v["keyword"]       = slug.replace("_", " ")
        v["drive_backed_up"] = (OUTPUT_DIR / slug / "drive_id.txt").exists() if slug else False
        v["watch_minutes"] = None   # per-video watch data requires Analytics API per-video query
        v["ctr"]           = None
        v["estimated_revenue"] = None
        v["short_url"]     = (f"https://youtube.com/shorts/{(OUTPUT_DIR / slug / 'short' / 'uploaded.txt').read_text().strip()}"
                               if slug and (OUTPUT_DIR / slug / "short" / "uploaded.txt").exists() else None)

    return {
        "channel":          channel_data,
        "videos":           video_stats,
        "channel_analytics": channel_analytics,
        "monetization":     monetization,
        "summary":          summary,
        "ypp":              ypp,
    }


@app.get("/api/settings")
async def get_settings() -> JSONResponse:
    import channel_settings
    return JSONResponse(channel_settings.load())


@app.post("/api/settings")
async def save_settings_endpoint(request: Request) -> JSONResponse:
    import channel_settings
    data = await request.json()
    channel_settings.save(data)
    await _broadcast({"type": "settings_saved", "settings": channel_settings.load()})
    return JSONResponse({"status": "saved"})


@app.get("/api/auto-reply/stats")
async def get_auto_reply_stats() -> JSONResponse:
    stats: dict[str, int] = {}
    if OUTPUT_DIR.exists():
        for d in OUTPUT_DIR.iterdir():
            if not d.is_dir():
                continue
            rf = d / "replied_comments.json"
            if rf.exists():
                try:
                    replied = json.loads(rf.read_text(encoding="utf-8"))
                    stats[d.name] = len(replied) if isinstance(replied, list) else 0
                except Exception:
                    stats[d.name] = 0
    return JSONResponse({"stats": stats})


@app.get("/api/analytics/per-video")
async def get_per_video_analytics() -> JSONResponse:
    """
    Fetch view/like/comment counts for all uploaded videos using
    YouTube Data API v3 (no Analytics API needed — just YOUTUBE_API_KEY).
    """
    vids = _scan_videos()
    uploaded = [(v["slug"], v["video_id"]) for v in vids if v.get("video_id")]
    if not uploaded:
        return JSONResponse({"videos": []})

    from config import YOUTUBE_API_KEY
    if not YOUTUBE_API_KEY:
        return JSONResponse({"videos": [], "error": "YOUTUBE_API_KEY not set"})

    try:
        from googleapiclient.discovery import build
        yt = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)
        id_to_slug = {vid_id: slug for slug, vid_id in uploaded}
        all_video_ids = list(id_to_slug.keys())
        result = []
        for i in range(0, len(all_video_ids), 50):
            batch = all_video_ids[i:i + 50]
            resp  = yt.videos().list(
                part="statistics,snippet,contentDetails", id=",".join(batch)
            ).execute()
            for item in resp.get("items", []):
                vid_id = item["id"]
                slug   = id_to_slug.get(vid_id, "")
                s      = item.get("statistics", {})
                dur    = item.get("contentDetails", {}).get("duration", "")
                # Parse ISO 8601 duration to seconds
                dur_s  = _parse_iso_duration(dur)
                # Count replied comments
                replies = 0
                rf = OUTPUT_DIR / slug / "replied_comments.json"
                if rf.exists():
                    try:
                        replies = len(json.loads(rf.read_text(encoding="utf-8")))
                    except Exception:
                        pass
                result.append({
                    "slug":          slug,
                    "video_id":      vid_id,
                    "title":         item["snippet"].get("title", ""),
                    "published_at":  item["snippet"].get("publishedAt", ""),
                    "thumbnail":     (item["snippet"].get("thumbnails", {})
                                      .get("medium", {}).get("url")),
                    "views":         int(s.get("viewCount", 0)),
                    "likes":         int(s.get("likeCount", 0)),
                    "comments":      int(s.get("commentCount", 0)),
                    "duration_s":    dur_s,
                    "has_short":     (OUTPUT_DIR / slug / "short" / "video.mp4").exists(),
                    "short_id":      ((OUTPUT_DIR / slug / "short" / "uploaded.txt")
                                       .read_text(encoding="utf-8").strip()
                                       if (OUTPUT_DIR / slug / "short" / "uploaded.txt").exists()
                                       else None),
                    "drive_backed_up": (OUTPUT_DIR / slug / "drive_id.txt").exists(),
                    "replies":       replies,
                })
        result.sort(key=lambda x: x.get("views", 0), reverse=True)
        return JSONResponse({"videos": result})
    except Exception as exc:
        logger.warning(f"per-video analytics failed: {exc}")
        return JSONResponse({"videos": [], "error": str(exc)})


def _parse_iso_duration(dur: str) -> int:
    """Convert ISO 8601 duration (PT4M13S) to total seconds."""
    import re as _re
    m = _re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", dur)
    if not m:
        return 0
    h, mn, s = (int(x or 0) for x in m.groups())
    return h * 3600 + mn * 60 + s


@app.post("/api/drive-backup/{slug}")
async def drive_backup_one(slug: str) -> JSONResponse:
    video_dir = OUTPUT_DIR / slug
    if not video_dir.exists():
        return JSONResponse({"error": "Video not found"}, status_code=404)
    if not (video_dir / "video.mp4").exists():
        return JSONResponse({"error": "video.mp4 not assembled yet"}, status_code=400)
    if (video_dir / "drive_id.txt").exists():
        fid = (video_dir / "drive_id.txt").read_text(encoding="utf-8").strip()
        return JSONResponse({"status": "already_backed_up", "drive_id": fid})
    asyncio.create_task(_drive_backup_task(slug, video_dir))
    return JSONResponse({"status": "started"})


@app.post("/api/drive-backup-all")
async def drive_backup_all_route() -> JSONResponse:
    pending = [
        d.name for d in OUTPUT_DIR.iterdir()
        if d.is_dir() and (d / "video.mp4").exists() and not (d / "drive_id.txt").exists()
    ]
    if not pending:
        return JSONResponse({"status": "nothing_to_backup"})
    asyncio.create_task(_drive_backup_all_task(pending))
    return JSONResponse({"status": "started", "count": len(pending)})


async def _drive_backup_task(slug: str, video_dir: Path) -> None:
    try:
        import json as _json
        title = slug.replace("_", " ")
        seo_path = video_dir / "seo.json"
        if seo_path.exists():
            try:
                title = _json.loads(seo_path.read_text(encoding="utf-8")).get("title", title)
            except Exception:
                pass
        await _broadcast({"type": "log", "level": "INFO",
                          "text": f"Drive backup starting: {title[:55]}"})
        from gdrive import upload_to_drive
        file_id = await asyncio.to_thread(upload_to_drive, video_dir / "video.mp4", title, video_dir)
        await _broadcast({"type": "drive_backup_complete", "slug": slug, "drive_id": file_id})
        await _broadcast({"type": "log", "level": "INFO",
                          "text": f"Drive backup complete — ID: {file_id}"})
    except Exception as exc:
        await _broadcast({"type": "log", "level": "ERROR",
                          "text": f"Drive backup failed ({slug}): {exc}"})


async def _drive_backup_all_task(slugs: list[str]) -> None:
    for slug in slugs:
        await _drive_backup_task(slug, OUTPUT_DIR / slug)


@app.post("/api/auto-reply/{slug}")
async def auto_reply(slug: str) -> JSONResponse:
    video_dir = OUTPUT_DIR / slug
    if not video_dir.exists():
        return JSONResponse({"error": "Video not found"}, status_code=404)
    uploaded = video_dir / "uploaded.txt"
    if not uploaded.exists():
        return JSONResponse({"error": "Video not uploaded yet"}, status_code=400)
    video_id = uploaded.read_text(encoding="utf-8").strip()
    asyncio.create_task(_auto_reply_task(slug, video_id, video_dir))
    return JSONResponse({"status": "started", "video_id": video_id})


async def _auto_reply_task(slug: str, video_id: str, video_dir: Path) -> None:
    try:
        await _broadcast({"type": "log", "level": "INFO",
                          "text": f"Auto-reply: fetching comments for {slug}…"})
        count = await asyncio.to_thread(_blocking_auto_reply, video_id, video_dir)
        await _broadcast({"type": "log", "level": "INFO",
                          "text": f"Auto-reply: posted {count} replies"})
        await _broadcast({"type": "auto_reply_done", "slug": slug, "count": count})
    except Exception as exc:
        await _broadcast({"type": "log", "level": "ERROR",
                          "text": f"Auto-reply failed ({slug}): {exc}"})


def _blocking_auto_reply(video_id: str, video_dir: Path) -> int:
    from auto_reply import reply_to_comments
    return reply_to_comments(video_id, video_dir)


@app.post("/api/upload-short/{slug}")
async def upload_short_endpoint(slug: str) -> JSONResponse:
    video_dir = OUTPUT_DIR / slug
    short_path = video_dir / "short" / "video.mp4"
    if not short_path.exists():
        return JSONResponse({"error": "Short video not found — generate it first"}, status_code=404)
    asyncio.create_task(_upload_short_task(slug, video_dir))
    return JSONResponse({"status": "started"})


async def _upload_short_task(slug: str, video_dir: Path) -> None:
    try:
        keyword = slug.replace("_", " ")
        if (video_dir / "seo.json").exists():
            import json as _json
            keyword = _json.loads((video_dir / "seo.json").read_text("utf-8")).get("title", keyword)[:60]
        short_path = video_dir / "short" / "video.mp4"
        await _broadcast({"type": "log", "level": "INFO",
                          "text": f"Uploading short for '{keyword[:50]}'…"})
        from shorts import upload_short
        video_id = await asyncio.to_thread(upload_short, short_path, keyword, video_dir)
        await _broadcast({"type": "short_done", "slug": slug, "video_id": video_id,
                          "url": f"https://youtube.com/shorts/{video_id}"})
        await _broadcast({"type": "log", "level": "INFO",
                          "text": f"Short uploaded → https://youtube.com/shorts/{video_id}"})
        try:
            from telegram_bot import notify_short_complete
            await asyncio.to_thread(notify_short_complete, keyword, video_id)
        except Exception:
            pass
    except Exception as exc:
        await _broadcast({"type": "log", "level": "ERROR",
                          "text": f"Short upload failed ({slug}): {exc}"})


@app.post("/api/make-short/{slug}")
async def make_short(slug: str) -> JSONResponse:
    video_dir = OUTPUT_DIR / slug
    if not video_dir.exists():
        return JSONResponse({"error": "Video not found"}, status_code=404)
    asyncio.create_task(_short_task(slug, video_dir))
    return JSONResponse({"status": "started"})


async def _short_task(slug: str, video_dir: Path) -> None:
    await _task_start(f"short:{slug}", "short", slug, f"Generating Short: {slug.replace('_',' ')}")
    try:
        keyword = slug.replace("_", " ")
        if (video_dir / "seo.json").exists():
            import json as _json
            keyword = _json.loads((video_dir / "seo.json").read_text("utf-8")).get("title", keyword)[:60]
        await _broadcast({"type": "log", "level": "INFO",
                          "text": f"Shorts: generating for '{keyword[:50]}'…"})
        from shorts import generate_short, upload_short
        short_path = await asyncio.to_thread(generate_short, keyword, video_dir)
        video_id   = await asyncio.to_thread(upload_short, short_path, keyword, video_dir)
        await _broadcast({"type": "short_done", "slug": slug, "video_id": video_id,
                          "url": f"https://youtube.com/shorts/{video_id}"})
        await _broadcast({"type": "log", "level": "INFO",
                          "text": f"Short uploaded → https://youtube.com/shorts/{video_id}"})
        # Telegram notification
        try:
            from telegram_bot import notify_short_complete
            await asyncio.to_thread(notify_short_complete, keyword, video_id)
        except Exception:
            pass
    except Exception as exc:
        await _broadcast({"type": "log", "level": "ERROR",
                          "text": f"Short failed ({slug}): {exc}"})
    finally:
        await _task_end(f"short:{slug}")


@app.get("/api/sheets/url")
async def sheets_url() -> JSONResponse:
    try:
        from sheets import get_sheet_url
        url = get_sheet_url()
        return JSONResponse({"url": url})
    except Exception as exc:
        return JSONResponse({"url": "", "error": str(exc)})


@app.post("/api/sheets/sync/{slug}")
async def sheets_sync(slug: str) -> JSONResponse:
    video_dir = OUTPUT_DIR / slug
    if not video_dir.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    asyncio.create_task(_sheets_sync_task(slug, video_dir))
    return JSONResponse({"status": "started"})


async def _sheets_sync_task(slug: str, video_dir: Path) -> None:
    try:
        keyword = slug.replace("_", " ")
        from sheets import sync_video
        url = await asyncio.to_thread(sync_video, keyword, video_dir)
        await _broadcast({"type": "sheets_sync_done", "slug": slug, "url": url})
        await _broadcast({"type": "log", "level": "INFO",
                          "text": f"Sheets synced for {slug}: {url}"})
    except Exception as exc:
        await _broadcast({"type": "log", "level": "ERROR",
                          "text": f"Sheets sync failed ({slug}): {exc}"})


@app.post("/api/sheets/sync-all")
async def sheets_sync_all() -> JSONResponse:
    asyncio.create_task(_sheets_sync_all_task())
    return JSONResponse({"status": "started"})


async def _sheets_sync_all_task() -> None:
    from sheets import sync_video
    from config import OUTPUT_DIR as _od
    count = 0
    for d in sorted(_od.iterdir()):
        if d.is_dir() and (d / "seo.json").exists():
            try:
                await asyncio.to_thread(sync_video, d.name.replace("_", " "), d)
                count += 1
            except Exception as exc:
                logger.warning(f"Sheets sync failed for {d.name}: {exc}")
    await _broadcast({"type": "log", "level": "INFO",
                      "text": f"Sheets: synced {count} videos"})


@app.post("/api/telegram/test")
async def telegram_test() -> JSONResponse:
    try:
        from telegram_bot import test_connection
        ok = await asyncio.to_thread(test_connection)
        return JSONResponse({"ok": ok})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)})


@app.get("/api/optimizer/ranks/all")
async def optimizer_ranks() -> JSONResponse:
    try:
        from channel_optimizer import get_all_ranks
        return JSONResponse(get_all_ranks())
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/optimizer/{slug}")
async def optimizer_audit(slug: str) -> JSONResponse:
    video_dir = OUTPUT_DIR / slug
    if not video_dir.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        from channel_optimizer import run_full_audit
        uploaded_p = video_dir / "uploaded.txt"
        video_id   = uploaded_p.read_text(encoding="utf-8").strip() if uploaded_p.exists() else None
        keyword    = slug.replace("_", " ")
        result     = await asyncio.to_thread(run_full_audit, video_dir, keyword, video_id)
        return JSONResponse(result)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


_SCHEDULE_FILE = BASE / "schedule.json"

@app.get("/api/schedule")
async def get_schedule() -> JSONResponse:
    if _SCHEDULE_FILE.exists():
        return JSONResponse(json.loads(_SCHEDULE_FILE.read_text(encoding="utf-8")))
    return JSONResponse({"time": "09:00", "count": 1, "enabled": False})

@app.post("/api/schedule")
async def save_schedule(request: Request) -> JSONResponse:
    data = await request.json()
    _SCHEDULE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return JSONResponse({"status": "saved"})


@app.post("/api/run")
async def start_run(count: int = 1, dry_run: bool = False) -> JSONResponse:
    global _running
    if _running:
        return JSONResponse({"error": "Pipeline already running"}, status_code=409)
    _running = True
    asyncio.create_task(_pipeline_task(count, dry_run))
    return JSONResponse({"status": "started", "count": count, "dry_run": dry_run})


# ── WebSocket ──────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    _ws_clients.append(ws)
    try:
        # Send current state immediately on connect
        channel: dict = {}
        if YOUTUBE_TOKEN_FILE.exists():
            try:
                channel = await asyncio.to_thread(_fetch_channel_info)
            except Exception:
                pass
        stype = _secrets_type()
        await ws.send_json({
            "type":          "init",
            "running":       _running,
            "videos":        _scan_videos(),
            "token_ok":      YOUTUBE_TOKEN_FILE.exists(),
            "secrets_ok":    stype == "installed",
            "secrets_type":  stype,
            "oauth_running": _oauth_running,
            "channel":       channel or None,
        })
        # Keep connection alive; ignore any client messages
        while True:
            try:
                await asyncio.wait_for(ws.receive_text(), timeout=25.0)
            except asyncio.TimeoutError:
                await ws.send_json({"type": "ping"})
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        if ws in _ws_clients:
            _ws_clients.remove(ws)


# ── Pipeline subprocess ────────────────────────────────────────────────────────

async def _pipeline_task(count: int, dry_run: bool) -> None:
    global _running, _stop_requested, _current_keyword
    _stop_requested = False
    _current_keyword = ""
    try:
        cmd = [sys.executable, "-u", str(BASE / "master.py"), "--count", str(count)]
        if dry_run:
            cmd.append("--dry-run")

        await _broadcast({"type": "pipeline_start", "count": count, "dry_run": dry_run})

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(BASE),
        )

        async for raw in proc.stdout:  # type: ignore[union-attr]
            text = raw.decode("utf-8", errors="replace").rstrip()
            if not text:
                continue
            level = (
                "ERROR"   if any(x in text for x in ("[ERROR]", "[CRITICAL]")) else
                "WARNING" if "[WARNING]" in text else
                "INFO"
            )
            await _broadcast({"type": "log", "level": level, "text": text})
            # Track current keyword server-side for Telegram /status
            kw_match = re.search(r"VIDEO \d+:\s+(.+)$", text)
            if kw_match:
                _current_keyword = kw_match.group(1).strip()
            # Stop-requested: terminate subprocess after current video finishes
            if _stop_requested and "Pipeline complete:" in text:
                proc.terminate()

        await proc.wait()
        _current_keyword = ""
        vids = _scan_videos()
        await _broadcast({
            "type":      "pipeline_end",
            "exit_code": proc.returncode,
            "videos":    vids,
        })
        # Daily Telegram summary after any completed run
        try:
            from telegram_bot import notify_daily_summary
            uploaded = sum(1 for v in vids if v.get("video_id"))
            assembled = sum(1 for v in vids if v.get("stages", {}).get("assemble"))
            ca = _cached_analytics.get("channel_analytics", {})
            await asyncio.to_thread(notify_daily_summary, {
                "assembled":   assembled,
                "uploaded":    uploaded,
                "views":       ca.get("views", 0),
                "subs":        _cached_analytics.get("channel", {}).get("subscribers", 0),
                "watch_hours": round(ca.get("estimatedMinutesWatched", 0) / 60, 1),
                "ypp_pct":     min(100.0, round(ca.get("estimatedMinutesWatched", 0) / 60 / 4000 * 100, 1)),
            })
        except Exception:
            pass
    except Exception as exc:
        await _broadcast({"type": "log", "level": "ERROR", "text": f"Dashboard error: {exc}"})
        await _broadcast({"type": "pipeline_end", "exit_code": -1, "videos": _scan_videos()})
    finally:
        _running = False
        _current_keyword = ""


# ── Pipeline stop ──────────────────────────────────────────────────────────────

@app.get("/api/logs")
async def get_logs() -> JSONResponse:
    """Return the last 300 lines of log.txt."""
    log_file = BASE / "log.txt"
    if not log_file.exists():
        return JSONResponse({"lines": []})
    try:
        lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
        return JSONResponse({"lines": lines[-300:]})
    except Exception as exc:
        return JSONResponse({"lines": [], "error": str(exc)})


@app.get("/api/logs/download")
async def download_logs():
    """Download the full log.txt file."""
    from fastapi.responses import FileResponse
    log_file = BASE / "log.txt"
    if not log_file.exists():
        return JSONResponse({"error": "No log file"}, status_code=404)
    return FileResponse(str(log_file), filename="ytauto.log", media_type="text/plain")


@app.get("/api/system/info")
async def system_info() -> JSONResponse:
    """Return system health info."""
    import platform
    import shutil
    disk = shutil.disk_usage(str(BASE))
    videos = _scan_videos()
    uploaded  = sum(1 for v in videos if v.get("video_id"))
    has_short = sum(1 for v in videos if v.get("has_short"))
    return JSONResponse({
        "platform":       platform.system(),
        "python_version": platform.python_version(),
        "disk_free_gb":   round(disk.free / 1e9, 1),
        "disk_used_gb":   round(disk.used / 1e9, 1),
        "disk_total_gb":  round(disk.total / 1e9, 1),
        "output_dir":     str(OUTPUT_DIR),
        "videos_total":   len(videos),
        "videos_uploaded": uploaded,
        "shorts_total":   has_short,
        "token_ok":       YOUTUBE_TOKEN_FILE.exists(),
        "secrets_ok":     _secrets_type() == "installed",
        "pipeline_running": _running,
    })


@app.post("/api/pipeline/stop")
async def pipeline_stop() -> JSONResponse:
    global _stop_requested
    if not _running:
        return JSONResponse({"status": "not_running"})
    _stop_requested = True
    await _broadcast({"type": "log", "level": "WARNING",
                      "text": "⛔ Stop requested — will halt after current video."})
    return JSONResponse({"status": "stop_requested"})


# ── Built-in scheduler ────────────────────────────────────────────────────────

async def _scheduler_loop() -> None:
    """
    Background loop: reads schedule.json every 60s and fires the pipeline
    when the configured time matches and enabled=True.
    """
    global _running
    import datetime as _dt
    _last_fired: str = ""   # "YYYY-MM-DD HH:MM" of the last trigger

    while True:
        try:
            await asyncio.sleep(60)
            sf = BASE / "schedule.json"
            if not sf.exists():
                continue
            try:
                cfg = json.loads(sf.read_text(encoding="utf-8"))
            except Exception:
                continue

            if not cfg.get("enabled", False):
                continue

            sched_time = cfg.get("time", "09:00")  # "HH:MM"
            now        = _dt.datetime.now().strftime("%H:%M")
            today_key  = _dt.datetime.now().strftime("%Y-%m-%d ") + sched_time

            if now == sched_time and today_key != _last_fired and not _running:
                _last_fired = today_key
                count = int(cfg.get("count", 1))
                logger.info(f"Scheduler: firing pipeline count={count} at {sched_time}")
                await _broadcast({"type": "log", "level": "INFO",
                                  "text": f"⏰ Scheduler: auto-starting pipeline ({count} video(s))"})
                _running = True
                asyncio.create_task(_pipeline_task(count, False))

                # Send Telegram notification
                try:
                    from telegram_bot import notify_pipeline_start
                    await asyncio.to_thread(notify_pipeline_start, count)
                except Exception:
                    pass

        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.debug(f"Scheduler loop error: {exc}")


# ── Bulk operations ────────────────────────────────────────────────────────────

@app.post("/api/bulk/upload-all")
async def bulk_upload_all() -> JSONResponse:
    """Queue upload for every assembled-but-not-uploaded video."""
    pending = [v for v in _scan_videos()
               if v.get("stages", {}).get("assemble") and not v.get("video_id") and not v.get("stages", {}).get("upload")]
    if not pending:
        return JSONResponse({"status": "nothing_to_upload", "count": 0})
    for v in pending:
        asyncio.create_task(_manual_upload_task(v["slug"], OUTPUT_DIR / v["slug"]))
    return JSONResponse({"status": "started", "count": len(pending),
                         "slugs": [v["slug"] for v in pending]})


@app.post("/api/bulk/short-all")
async def bulk_short_all() -> JSONResponse:
    """Generate a Short for every video that doesn't have one yet."""
    pending = [v for v in _scan_videos()
               if v.get("stages", {}).get("assemble") and not v.get("has_short")]
    if not pending:
        return JSONResponse({"status": "nothing_to_do", "count": 0})
    for v in pending:
        asyncio.create_task(_short_task(v["slug"], OUTPUT_DIR / v["slug"]))
    return JSONResponse({"status": "started", "count": len(pending),
                         "slugs": [v["slug"] for v in pending]})


@app.post("/api/bulk/reply-all")
async def bulk_reply_all() -> JSONResponse:
    """Auto-reply to comments on every uploaded video."""
    uploaded = [v for v in _scan_videos() if v.get("video_id")]
    if not uploaded:
        return JSONResponse({"status": "nothing_to_do", "count": 0})
    for v in uploaded:
        asyncio.create_task(_auto_reply_task(v["slug"], v["video_id"], OUTPUT_DIR / v["slug"]))
    return JSONResponse({"status": "started", "count": len(uploaded)})


@app.get("/api/video/{slug}/detail")
async def video_detail(slug: str) -> JSONResponse:
    """Return full metadata for a single video: SEO, script preview, stats."""
    video_dir = OUTPUT_DIR / slug
    if not video_dir.exists():
        return JSONResponse({"error": "not found"}, status_code=404)

    detail: dict[str, Any] = {"slug": slug, "keyword": slug.replace("_", " ")}

    # SEO
    seo_path = video_dir / "seo.json"
    if seo_path.exists():
        try:
            detail["seo"] = json.loads(seo_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Script preview (first 600 chars)
    script_path = video_dir / "script.txt"
    if script_path.exists():
        try:
            full = script_path.read_text(encoding="utf-8")
            detail["script_preview"] = full[:800]
            detail["script_words"]   = len(full.split())
        except Exception:
            pass

    # Short script preview
    short_script = video_dir / "short" / "script.txt"
    if short_script.exists():
        try:
            detail["short_script_preview"] = short_script.read_text(encoding="utf-8")[:400]
        except Exception:
            pass

    # File sizes
    for fname, key in [("video.mp4", "video_mb"), ("voice.mp3", "voice_mb"),
                        ("thumbnail.jpg", "thumbnail_kb")]:
        p = video_dir / fname
        if p.exists():
            size = p.stat().st_size
            detail[key] = round(size / (1024 * 1024 if fname.endswith(".mp4") else 1024), 1)

    # Rank history
    hist = video_dir / "rank_history.json"
    if hist.exists():
        try:
            detail["rank_history"] = json.loads(hist.read_text(encoding="utf-8"))[-7:]
        except Exception:
            pass

    # Upload info
    if (video_dir / "uploaded.txt").exists():
        detail["video_id"] = (video_dir / "uploaded.txt").read_text(encoding="utf-8").strip()
    if (video_dir / "short" / "uploaded.txt").exists():
        detail["short_video_id"] = (video_dir / "short" / "uploaded.txt").read_text(encoding="utf-8").strip()

    return JSONResponse(detail)


# ── Keyword queue ─────────────────────────────────────────────────────────────

@app.get("/api/keywords/queue")
async def get_keyword_queue() -> JSONResponse:
    from keywords_queue import load_queue
    return JSONResponse({"queue": load_queue()})


@app.post("/api/keywords/queue")
async def add_to_keyword_queue(req: Request) -> JSONResponse:
    from keywords_queue import add_keywords, load_queue
    body = await req.json()
    keywords = body.get("keywords") or ([body["keyword"]] if body.get("keyword") else [])
    keywords = [k.strip() for k in keywords if k.strip()]
    if not keywords:
        return JSONResponse({"error": "no keywords provided"}, status_code=400)
    added = add_keywords(keywords, added_by="user")
    q = load_queue()
    await _broadcast({"type": "queue_update", "queue": q})
    return JSONResponse({"added": added, "queue": q})


@app.delete("/api/keywords/queue/{keyword}")
async def remove_from_keyword_queue(keyword: str) -> JSONResponse:
    from urllib.parse import unquote
    from keywords_queue import remove_keyword, load_queue
    removed = remove_keyword(unquote(keyword))
    q = load_queue()
    await _broadcast({"type": "queue_update", "queue": q})
    return JSONResponse({"removed": removed, "queue": q})


@app.post("/api/keywords/research")
async def trigger_keyword_research() -> JSONResponse:
    asyncio.create_task(_research_task())
    return JSONResponse({"status": "started"})


async def _research_task() -> None:
    await _task_start("research", "research", "", "Running keyword research…")
    await _broadcast({"type": "log", "level": "INFO",
                      "text": "🔍 Keyword research starting — fetching trends…"})
    try:
        from research import get_trending_keywords
        from keywords_queue import add_keywords, load_queue
        keywords = await asyncio.to_thread(get_trending_keywords, 10)
        added = add_keywords(keywords, added_by="research")
        q = load_queue()
        await _broadcast({"type": "queue_update", "queue": q})
        await _broadcast({"type": "log", "level": "INFO",
                          "text": f"🔍 Research done — {added} new keyword(s) added to queue"})
    except Exception as exc:
        await _broadcast({"type": "log", "level": "ERROR",
                          "text": f"Keyword research failed: {exc}"})
    finally:
        await _task_end("research")


@app.get("/api/tasks")
async def get_active_tasks() -> JSONResponse:
    return JSONResponse({"tasks": list(_active_tasks.values())})


# ── Thumbnail regeneration ─────────────────────────────────────────────────────

@app.post("/api/regen/thumbnail/{slug}")
async def regen_thumbnail(slug: str) -> JSONResponse:
    video_dir = OUTPUT_DIR / slug
    if not video_dir.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    if not (video_dir / "seo.json").exists():
        return JSONResponse({"error": "no seo.json — run pipeline first"}, status_code=400)
    asyncio.create_task(_regen_thumbnail_task(slug, video_dir))
    return JSONResponse({"status": "started"})


async def _regen_thumbnail_task(slug: str, video_dir: Path) -> None:
    await _task_start(f"thumb:{slug}", "thumbnail", slug, f"Regenerating thumbnail: {slug.replace('_', ' ')}")
    await _broadcast({"type": "log", "level": "INFO",
                      "text": f"Thumbnail regen: starting for {slug}…"})
    try:
        # Remove existing thumbnail so the generator runs fresh
        old_thumb = video_dir / "thumbnail.jpg"
        if old_thumb.exists():
            old_thumb.unlink()

        seo_data = json.loads((video_dir / "seo.json").read_text(encoding="utf-8"))
        keyword  = seo_data.get("title", slug.replace("_", " "))

        from thumbnail import create_thumbnail
        await asyncio.to_thread(create_thumbnail, keyword, video_dir)

        await _broadcast({"type": "thumbnail_done", "slug": slug})
        await _broadcast({"type": "log", "level": "INFO",
                          "text": f"Thumbnail regenerated for {slug}"})
    except Exception as exc:
        await _broadcast({"type": "log", "level": "ERROR",
                          "text": f"Thumbnail regen failed ({slug}): {exc}"})
    finally:
        await _task_end(f"thumb:{slug}")


# ── View-milestone tracker ────────────────────────────────────────────────────

_MILESTONES   = [100, 500, 1000, 5000, 10000, 50000, 100_000]
_MILESTONE_FILE = BASE / "milestones.json"


async def _milestone_checker_loop() -> None:
    """Check every 4 hours whether any video has crossed a new view milestone."""
    while True:
        try:
            await asyncio.sleep(4 * 3600)
            await asyncio.to_thread(_check_milestones_sync)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.debug(f"Milestone checker error: {exc}")


def _check_milestones_sync() -> None:
    from config import YOUTUBE_API_KEY
    if not YOUTUBE_API_KEY:
        return

    history: dict[str, list[int]] = {}
    if _MILESTONE_FILE.exists():
        try:
            history = json.loads(_MILESTONE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass

    vids = _scan_videos()
    uploaded = [(v["slug"], v["video_id"]) for v in vids if v.get("video_id")]
    if not uploaded:
        return

    try:
        from googleapiclient.discovery import build
        yt      = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)
        id_map  = {vid_id: slug for slug, vid_id in uploaded}
        vid_ids = list(id_map.keys())

        for i in range(0, len(vid_ids), 50):
            batch = vid_ids[i:i + 50]
            resp  = yt.videos().list(part="statistics,snippet", id=",".join(batch)).execute()
            for item in resp.get("items", []):
                vid_id = item["id"]
                views  = int(item["statistics"].get("viewCount", 0))
                title  = item["snippet"].get("title", vid_id)[:60]
                reached = history.setdefault(vid_id, [])
                for m in _MILESTONES:
                    if views >= m and m not in reached:
                        reached.append(m)
                        try:
                            from telegram_bot import notify_milestone
                            notify_milestone(title, views, m, vid_id)
                            logger.info(f"Milestone: '{title}' hit {m:,} views")
                        except Exception:
                            pass

        _MILESTONE_FILE.write_text(json.dumps(history, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.debug(f"Milestone check failed: {exc}")


@app.post("/api/milestones/check")
async def trigger_milestone_check() -> JSONResponse:
    """Manually trigger a milestone check."""
    asyncio.create_task(asyncio.to_thread(_check_milestones_sync))
    return JSONResponse({"status": "started"})


@app.get("/api/milestones")
async def get_milestones() -> JSONResponse:
    history: dict = {}
    if _MILESTONE_FILE.exists():
        try:
            history = json.loads(_MILESTONE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return JSONResponse({"milestones": history})


# ── CSV export ────────────────────────────────────────────────────────────────

@app.get("/api/analytics/export.csv")
async def export_analytics_csv():
    from fastapi.responses import StreamingResponse
    import io, csv as _csv
    vids = _scan_videos()
    out  = io.StringIO()
    w    = _csv.writer(out)
    w.writerow(["Slug", "Keyword", "Upload Date", "Video ID", "YT Link",
                "Has Short", "Short ID", "Short Link", "Drive Backed Up",
                "Stages Done"])
    for v in vids:
        stages_done = sum(1 for s in (v.get("stages") or {}).values() if s)
        vid_id  = v.get("video_id") or ""
        short_id = v.get("short_video_id") or ""
        w.writerow([
            v.get("slug", ""),
            v.get("keyword", ""),
            v.get("upload_date") or "",
            vid_id,
            f"https://youtube.com/watch?v={vid_id}" if vid_id else "",
            "Yes" if v.get("has_short") else "No",
            short_id,
            f"https://youtube.com/shorts/{short_id}" if short_id else "",
            "Yes" if v.get("drive_backed_up") else "No",
            stages_done,
        ])
    out.seek(0)
    return StreamingResponse(
        iter([out.read()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=ytauto_videos.csv"},
    )


# ── Telegram command listener ──────────────────────────────────────────────────

async def _start_telegram_listener() -> None:
    """Background task: start Telegram command listener with dashboard callbacks."""
    from config import TELEGRAM_BOT_TOKEN as _token
    if not _token:
        return

    try:
        from telegram_commands import start_listener
        from pathlib import Path as _Path

        def _get_status() -> dict:
            return {
                "running":         _running,
                "current_keyword": _current_keyword,
                "videos":          _scan_videos(),
            }

        async def _trigger_run(count: int, dry_run: bool) -> None:
            global _running
            if _running:
                return
            _running = True
            asyncio.create_task(_pipeline_task(count, dry_run))

        async def _trigger_short(keyword_or_slug: str) -> None:
            # Find matching output dir: try exact slug first, then fuzzy
            slug = re.sub(r"[^\w\s-]", "", keyword_or_slug.lower())
            slug = re.sub(r"\s+", "_", slug.strip())[:60]
            video_dir = OUTPUT_DIR / slug
            if not video_dir.exists():
                # Fuzzy: find first folder whose name contains all words
                words = slug.split("_")[:3]
                for d in sorted(OUTPUT_DIR.iterdir()):
                    if d.is_dir() and all(w in d.name for w in words):
                        video_dir = d
                        slug = d.name
                        break
            if not video_dir.exists():
                from telegram_bot import send
                await asyncio.to_thread(
                    send, f"❌ No video found for: <i>{keyword_or_slug[:60]}</i>\n"
                          "Use /videos to see available keywords.")
                return
            asyncio.create_task(_short_task(slug, video_dir))

        def _trigger_stop() -> None:
            global _stop_requested
            _stop_requested = True

        def _get_analytics() -> dict:
            return _cached_analytics

        def _get_schedule() -> dict:
            _sf = BASE / "schedule.json"
            if _sf.exists():
                try:
                    return json.loads(_sf.read_text(encoding="utf-8"))
                except Exception:
                    pass
            return {"time": "09:00", "count": 1, "enabled": False}

        await start_listener(
            get_status=_get_status,
            trigger_run=_trigger_run,
            trigger_short=_trigger_short,
            get_analytics=_get_analytics,
            trigger_stop=_trigger_stop,
            get_schedule=_get_schedule,
        )
    except Exception as exc:
        logger.warning(f"Telegram listener failed to start: {exc}")


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import webbrowser
    import uvicorn

    print("=" * 55)
    print("  ytauto Dashboard  ->  http://127.0.0.1:8000")
    print("  Press Ctrl+C to stop")
    print("=" * 55)
    webbrowser.open("http://127.0.0.1:8000")
    uvicorn.run("dashboard:app", host="127.0.0.1", port=8000, reload=False)
