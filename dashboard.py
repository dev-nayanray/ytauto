"""
Visual dashboard for the ytauto pipeline.

Run:  python dashboard.py
Open: http://127.0.0.1:8000
"""
import asyncio
import json
import logging
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

app = FastAPI(title="ytauto Dashboard")
app.mount("/output", StaticFiles(directory=str(OUTPUT_DIR)), name="output")
_tmpl = Jinja2Templates(directory=str(BASE / "templates"))

_ws_clients: list[WebSocket] = []
_running: bool = False
_oauth_running: bool = False


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
        "thumbnail":        snippet.get("thumbnails", {}).get("default", {}).get("url"),
        "subscriber_count": int(stats.get("subscriberCount", 0)),
        "video_count":      int(stats.get("videoCount", 0)),
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


def _blocking_upload(
    video_path: Path, thumb_path: Path,
    seo: dict, publish_at: Any, output_dir: Path,
) -> str:
    from upload import upload_video
    return upload_video(video_path, thumb_path, seo, publish_at, output_dir)


@app.get("/api/youtube/analytics")
async def get_youtube_analytics() -> JSONResponse:
    if not YOUTUBE_TOKEN_FILE.exists():
        return JSONResponse({"connected": False, "reason": "no_token"})
    try:
        data = await asyncio.to_thread(_fetch_full_analytics)
        return JSONResponse({"connected": True, **data})
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
    return {
        "channel": channel_data,
        "videos": video_stats,
        "channel_analytics": channel_analytics,
        "monetization": monetization,
    }


@app.get("/api/settings")
async def get_settings() -> JSONResponse:
    env_path = BASE / ".env"
    settings: dict[str, str] = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            is_secret = any(x in key.upper() for x in ["KEY", "SECRET", "TOKEN", "PASSWORD"])
            if is_secret and len(val) > 8:
                settings[key] = val[:4] + "…" + val[-4:]
            else:
                settings[key] = val
    return JSONResponse(settings)


@app.post("/api/settings")
async def save_settings(request: Request) -> JSONResponse:
    data = await request.json()
    env_path = BASE / ".env"
    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    updated: set[str] = set()
    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            new_lines.append(line)
            continue
        key = stripped.partition("=")[0].strip()
        if key in data and data[key] and "…" not in str(data[key]):
            new_lines.append(f"{key}={data[key]}")
            updated.add(key)
        else:
            new_lines.append(line)
    for key, val in data.items():
        if key not in updated and val and "…" not in str(val):
            new_lines.append(f"{key}={val}")
    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    return JSONResponse({"status": "saved", "updated": list(updated)})


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


@app.post("/api/make-short/{slug}")
async def make_short(slug: str) -> JSONResponse:
    video_dir = OUTPUT_DIR / slug
    if not video_dir.exists():
        return JSONResponse({"error": "Video not found"}, status_code=404)
    asyncio.create_task(_short_task(slug, video_dir))
    return JSONResponse({"status": "started"})


async def _short_task(slug: str, video_dir: Path) -> None:
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
    except Exception as exc:
        await _broadcast({"type": "log", "level": "ERROR",
                          "text": f"Short failed ({slug}): {exc}"})


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


@app.get("/api/optimizer/ranks/all")
async def optimizer_ranks() -> JSONResponse:
    try:
        from channel_optimizer import get_all_ranks
        return JSONResponse(get_all_ranks())
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


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
    global _running
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

        await proc.wait()
        await _broadcast({
            "type":      "pipeline_end",
            "exit_code": proc.returncode,
            "videos":    _scan_videos(),
        })
    except Exception as exc:
        await _broadcast({"type": "log", "level": "ERROR", "text": f"Dashboard error: {exc}"})
        await _broadcast({"type": "pipeline_end", "exit_code": -1, "videos": _scan_videos()})
    finally:
        _running = False


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
