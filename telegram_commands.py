"""
Telegram command bot — full pipeline control via chat commands.

Commands:
  /start          Welcome message
  /help           List all commands
  /status         Pipeline status (running / idle, current keyword)
  /run [N] [dry]  Start pipeline (N videos, optional dry-run)
  /stop           Ask the pipeline to stop after the current video
  /stats          Channel analytics summary
  /videos         List last 5 videos with YouTube links
  /shorts         List all generated YouTube Shorts
  /short <kw>     Generate a Short for a video by keyword or slug
  /schedule       Show current schedule config
"""
import asyncio
import logging
import re
from typing import Callable, Awaitable, Any

import requests

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# Commands shown in Telegram's menu
_COMMANDS = [
    {"command": "start",    "description": "Welcome message"},
    {"command": "help",     "description": "All available commands"},
    {"command": "status",   "description": "Pipeline status"},
    {"command": "run",      "description": "Start pipeline  /run 2  or  /run 2 dry"},
    {"command": "stop",     "description": "Stop pipeline after current video"},
    {"command": "stats",    "description": "Channel analytics"},
    {"command": "videos",   "description": "Recent videos list"},
    {"command": "shorts",   "description": "Generated Shorts list"},
    {"command": "short",    "description": "Generate short  /short <keyword>"},
    {"command": "schedule", "description": "Show schedule config"},
]


# ── Low-level HTTP ─────────────────────────────────────────────────────────────

def _get(method: str, **kwargs) -> dict:
    if not TELEGRAM_BOT_TOKEN:
        return {}
    try:
        r = requests.get(f"{_BASE}/{method}", timeout=35, **kwargs)
        return r.json() if r.ok else {}
    except Exception as exc:
        logger.debug(f"Telegram GET {method}: {exc}")
        return {}


def _post(method: str, **kwargs) -> dict:
    if not TELEGRAM_BOT_TOKEN:
        return {}
    try:
        r = requests.post(f"{_BASE}/{method}", timeout=10, **kwargs)
        return r.json() if r.ok else {}
    except Exception as exc:
        logger.debug(f"Telegram POST {method}: {exc}")
        return {}


def _reply(chat_id: int | str, text: str, parse_mode: str = "HTML",
           disable_preview: bool = True) -> None:
    _post("sendMessage", json={
        "chat_id":                  chat_id,
        "text":                     text,
        "parse_mode":               parse_mode,
        "disable_web_page_preview": disable_preview,
    })


# ── Command listener ───────────────────────────────────────────────────────────

async def start_listener(
    get_status:    Callable[[], dict],
    trigger_run:   Callable[[int, bool], Awaitable[None]],
    trigger_short: Callable[[str], Awaitable[None]],
    get_analytics: Callable[[], dict],
    trigger_stop:  Callable[[], None],
    get_schedule:  Callable[[], dict],
) -> None:
    """
    Long-poll Telegram and dispatch commands.
    Call as an asyncio background task from dashboard.py startup.
    """
    if not TELEGRAM_BOT_TOKEN:
        logger.info("TELEGRAM_BOT_TOKEN not set — command listener disabled.")
        return

    # Register menu commands with Telegram
    await asyncio.to_thread(_post, "setMyCommands", json={"commands": _COMMANDS})
    logger.info("Telegram command listener started.")

    offset = 0
    while True:
        try:
            data = await asyncio.to_thread(
                _get, "getUpdates",
                params={
                    "offset":           offset,
                    "timeout":          25,
                    "allowed_updates":  ["message"],
                },
            )
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                msg     = update.get("message", {})
                text    = msg.get("text", "").strip()
                chat_id = msg.get("chat", {}).get("id")
                if not text or not chat_id:
                    continue
                if str(chat_id) != str(TELEGRAM_CHAT_ID):
                    _reply(chat_id, "⛔ Unauthorized.")
                    continue
                await _dispatch(
                    text, chat_id,
                    get_status, trigger_run, trigger_short,
                    get_analytics, trigger_stop, get_schedule,
                )
        except asyncio.CancelledError:
            logger.info("Telegram command listener stopped.")
            break
        except Exception as exc:
            logger.debug(f"Telegram poll loop error: {exc}")
            await asyncio.sleep(5)


async def _dispatch(
    text: str,
    chat_id: int | str,
    get_status:    Callable,
    trigger_run:   Callable,
    trigger_short: Callable,
    get_analytics: Callable,
    trigger_stop:  Callable,
    get_schedule:  Callable,
) -> None:
    parts = text.split()
    raw_cmd = parts[0].lower().lstrip("/").split("@")[0]
    args = parts[1:]

    if raw_cmd == "start":
        _reply(chat_id,
            "🤖 <b>ytauto Bot Ready!</b>\n\n"
            "I control your YouTube automation pipeline.\n"
            "Send /help to see all commands.\n\n"
            "<i>Only authorized for your chat ID.</i>")

    elif raw_cmd == "help":
        _reply(chat_id,
            "📋 <b>Available Commands</b>\n\n"
            "/status — Pipeline status\n"
            "/run [N] [dry] — Start pipeline\n"
            "  e.g. <code>/run 2</code> or <code>/run 1 dry</code>\n"
            "/stop — Stop after current video\n"
            "/stats — Channel analytics\n"
            "/videos — Recent 5 videos\n"
            "/shorts — All generated Shorts\n"
            "/short &lt;keyword&gt; — Make a Short\n"
            "  e.g. <code>/short chatgpt tips</code>\n"
            "/schedule — Current schedule\n"
            "/help — This message")

    elif raw_cmd == "status":
        s = get_status()
        running = s.get("running", False)
        kw = s.get("current_keyword", "")
        vcount = len(s.get("videos", []))
        shorts = sum(1 for v in s.get("videos", []) if v.get("has_short"))
        state_line = "🟢 <b>Running</b>" if running else "⚪ Idle"
        msg = (f"📊 <b>Pipeline Status</b>\n\n"
               f"State: {state_line}\n"
               f"Videos produced: <b>{vcount}</b>\n"
               f"Shorts generated: <b>{shorts}</b>")
        if kw:
            msg += f"\n\n⏳ Working on:\n<i>{kw}</i>"
        _reply(chat_id, msg)

    elif raw_cmd == "run":
        count = 1
        dry_run = False
        for a in args:
            if a.isdigit():
                count = min(int(a), 5)
            elif a.lower() in ("dry", "dryrun", "dry-run", "test"):
                dry_run = True
        s = get_status()
        if s.get("running"):
            _reply(chat_id, "⚠️ Pipeline is already running.\nUse /stop to finish after current video.")
        else:
            mode = " <i>(dry run — no upload)</i>" if dry_run else ""
            _reply(chat_id,
                f"🚀 <b>Pipeline starting!</b>{mode}\n"
                f"📹 Videos queued: <b>{count}</b>\n"
                f"⏳ Est. time: ~{count * 15} min")
            await trigger_run(count, dry_run)

    elif raw_cmd == "stop":
        trigger_stop()
        _reply(chat_id,
            "🛑 <b>Stop requested.</b>\n"
            "Pipeline will finish the current video then halt.")

    elif raw_cmd == "stats":
        analytics = get_analytics()
        if not analytics or analytics.get("connected") is False:
            _reply(chat_id,
                "📈 Analytics not available.\n"
                "Make sure YouTube is connected in the dashboard.")
            return
        s   = analytics.get("summary", {})
        ypp = analytics.get("ypp", {})
        wh  = ypp.get("watch_hours", 0)
        sub = ypp.get("subscribers", 0)
        _reply(chat_id,
            f"📈 <b>Channel Analytics (28 days)</b>\n\n"
            f"👁  Views:       <b>{s.get('total_views',0):,}</b>\n"
            f"⏱  Watch hours: <b>{s.get('watch_hours',0):.1f}h</b>\n"
            f"👥 Subscribers: <b>{s.get('subscribers',0):,}</b>\n"
            f"💰 Est. revenue: <b>${s.get('estimated_revenue',0):.2f}</b>\n\n"
            f"🏆 <b>YPP Progress</b>\n"
            f"  Watch: {wh:.0f} / 4000h  ({min(100,wh/40):.0f}%)\n"
            f"  Subs:  {sub} / 1000  ({min(100,sub/10):.0f}%)")

    elif raw_cmd == "videos":
        s = get_status()
        videos = s.get("videos", [])[:5]
        if not videos:
            _reply(chat_id, "🎬 No videos yet.\nUse /run to start the pipeline.")
            return
        lines = ["🎬 <b>Recent Videos</b>\n"]
        for v in videos:
            kw = (v.get("keyword") or v.get("slug") or "?")[:55]
            vid_id = v.get("video_id")
            short  = " ⚡" if v.get("has_short") else ""
            if vid_id:
                lines.append(f"• <a href='https://youtu.be/{vid_id}'>{kw}</a>{short}")
            else:
                lines.append(f"• {kw} <i>(not uploaded)</i>{short}")
        _reply(chat_id, "\n".join(lines))

    elif raw_cmd == "shorts":
        s = get_status()
        videos = s.get("videos", [])
        shorts = [v for v in videos if v.get("has_short")]
        if not shorts:
            _reply(chat_id,
                "⚡ No Shorts generated yet.\n"
                "Use <code>/short &lt;keyword&gt;</code> to create one.")
            return
        lines = [f"⚡ <b>Generated Shorts ({len(shorts)})</b>\n"]
        for v in shorts[:10]:
            kw  = (v.get("keyword") or v.get("slug") or "?")[:55]
            sid = v.get("short_video_id")
            if sid:
                lines.append(f"• <a href='https://youtube.com/shorts/{sid}'>{kw}</a>")
            else:
                lines.append(f"• {kw} <i>(generated, not uploaded)</i>")
        _reply(chat_id, "\n".join(lines))

    elif raw_cmd == "short":
        if not args:
            _reply(chat_id,
                "Usage: <code>/short &lt;keyword or slug&gt;</code>\n"
                "Example: <code>/short chatgpt tips 2025</code>\n\n"
                "Use /videos to see available video keywords.")
            return
        keyword_or_slug = " ".join(args)
        _reply(chat_id,
            f"⚡ Generating Short for:\n<i>{keyword_or_slug[:60]}</i>\n\n"
            "You'll get a notification when it's done.")
        await trigger_short(keyword_or_slug)

    elif raw_cmd == "schedule":
        sc = get_schedule()
        enabled = sc.get("enabled", False)
        time_   = sc.get("time", "09:00")
        count   = sc.get("count", 1)
        status  = "🟢 Enabled" if enabled else "🔴 Disabled"
        _reply(chat_id,
            f"⏰ <b>Schedule Config</b>\n\n"
            f"Status: {status}\n"
            f"Daily time: <code>{time_}</code>\n"
            f"Videos/run: <b>{count}</b>\n\n"
            f"<i>Change in the dashboard → Schedule section.</i>")

    else:
        _reply(chat_id,
            f"❓ Unknown command: <code>/{raw_cmd}</code>\n"
            "Send /help for the command list.")
