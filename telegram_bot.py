"""
Telegram notification bot for ytauto pipeline events.

Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env.
Get your token from @BotFather on Telegram.
Get your chat_id by messaging @userinfobot.

All functions are fire-and-forget — they never raise; errors are logged only.
"""
import logging
from pathlib import Path

import requests

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

_BASE = "https://api.telegram.org/bot{}/{}"  # format(token, method)


def _api(method: str, **kwargs) -> bool:
    """POST to Telegram Bot API. Returns True on success."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}",
            timeout=10,
            **kwargs,
        )
        if not r.ok:
            logger.warning(f"Telegram {method} failed: {r.text[:200]}")
        return r.ok
    except Exception as exc:
        logger.warning(f"Telegram request failed: {exc}")
        return False


def send(text: str, parse_mode: str = "HTML") -> bool:
    """Send a plain text message."""
    return _api("sendMessage", json={
        "chat_id":    TELEGRAM_CHAT_ID,
        "text":       text,
        "parse_mode": parse_mode,
    })


def send_photo(image_path: Path, caption: str = "") -> bool:
    """Send a photo with optional caption."""
    try:
        with open(image_path, "rb") as fh:
            return _api("sendPhoto", data={
                "chat_id":    TELEGRAM_CHAT_ID,
                "caption":    caption[:1024],
                "parse_mode": "HTML",
            }, files={"photo": fh})
    except Exception as exc:
        logger.warning(f"Telegram photo send failed: {exc}")
        return False


def send_document(file_path: Path, caption: str = "") -> bool:
    """Send a file as document."""
    try:
        with open(file_path, "rb") as fh:
            return _api("sendDocument", data={
                "chat_id":    TELEGRAM_CHAT_ID,
                "caption":    caption[:1024],
                "parse_mode": "HTML",
            }, files={"document": fh})
    except Exception as exc:
        logger.warning(f"Telegram document send failed: {exc}")
        return False


# ── Pipeline event helpers ─────────────────────────────────────────────────────

def notify_pipeline_start(count: int) -> None:
    send(f"🚀 <b>ytauto Pipeline Started</b>\n"
         f"📹 Videos queued: <b>{count}</b>\n"
         f"⏳ Est. time: ~{count * 15} min")


def notify_stage(keyword: str, stage: str, stage_num: int, total: int = 8) -> None:
    icons = {"research": "🔍", "script": "📝", "voice": "🎙", "visuals": "🎬",
             "assemble": "⚡", "seo": "📊", "thumbnail": "🖼", "upload": "🚀"}
    icon = icons.get(stage, "▶")
    bar  = "█" * stage_num + "░" * (total - stage_num)
    send(f"{icon} <b>{stage.title()}</b> — {keyword[:40]}\n"
         f"[{bar}] {stage_num}/{total}")


def notify_video_complete(keyword: str, title: str, video_id: str,
                           thumbnail_path: Path | None = None) -> None:
    url  = f"https://youtube.com/watch?v={video_id}"
    msg  = (f"✅ <b>Video Uploaded!</b>\n\n"
            f"🔑 <i>{keyword[:60]}</i>\n"
            f"📺 {title[:80]}\n\n"
            f"🔗 <a href='{url}'>Watch on YouTube</a>")
    if thumbnail_path and thumbnail_path.exists():
        send_photo(thumbnail_path, caption=msg)
    else:
        send(msg)


def notify_short_complete(keyword: str, video_id: str) -> None:
    url = f"https://youtube.com/shorts/{video_id}"
    send(f"⚡ <b>Short Uploaded!</b>\n"
         f"🔑 {keyword[:60]}\n"
         f"🔗 <a href='{url}'>Watch Short</a>")


def notify_drive_backup(keyword: str, folder_url: str) -> None:
    send(f"☁️ <b>Drive Backup Complete</b>\n"
         f"📁 {keyword[:60]}\n"
         f"🔗 <a href='{folder_url}'>Open in Drive</a>")


def notify_sheets_update(keyword: str, sheet_url: str) -> None:
    send(f"📊 <b>Sheets Updated</b>\n"
         f"🔑 {keyword[:60]}\n"
         f"🔗 <a href='{sheet_url}'>Open Sheet</a>")


def notify_error(keyword: str, stage: str, error: str) -> None:
    send(f"❌ <b>Pipeline Error</b>\n"
         f"🔑 {keyword[:50]}\n"
         f"📍 Stage: {stage}\n"
         f"💬 {str(error)[:300]}")


def notify_daily_summary(stats: dict) -> None:
    """Send daily stats summary."""
    send(
        f"📈 <b>Daily Summary — ytauto</b>\n\n"
        f"✅ Videos assembled: {stats.get('assembled', 0)}\n"
        f"📤 Videos uploaded:  {stats.get('uploaded', 0)}\n"
        f"👁  Total views:     {stats.get('views', 0):,}\n"
        f"👥 Subscribers:     {stats.get('subs', 0):,}\n"
        f"⏱  Watch hours:     {stats.get('watch_hours', 0):.1f}h\n"
        f"💰 YPP progress:    {stats.get('ypp_pct', 0):.1f}%"
    )


def notify_goal_complete(goal_title: str, target: float, unit: str) -> None:
    """Notify when a goal is completed."""
    send(
        f"🎯 <b>Goal Achieved!</b>\n\n"
        f"<b>{goal_title}</b>\n"
        f"Target: <b>{target:,.0f} {unit}</b> ✅\n\n"
        f"Great work — set a new goal to keep growing! 🚀"
    )


def notify_milestone(title: str, views: int, milestone: int, video_id: str) -> None:
    """Notify when a video reaches a view milestone."""
    emoji = "🏆" if milestone >= 10000 else "🎉" if milestone >= 1000 else "🌱"
    send(
        f"{emoji} <b>Milestone hit!</b>\n\n"
        f"<b>{title[:60]}</b>\n"
        f"just reached <b>{milestone:,} views</b> 🎊\n"
        f"(Current: {views:,} views)\n\n"
        f"<a href='https://youtube.com/watch?v={video_id}'>Watch video →</a>"
    )


def test_connection() -> bool:
    """Send a test message. Returns True if successful."""
    ok = send("🤖 <b>ytauto Bot Connected!</b>\n✅ Notifications are working.")
    return ok
