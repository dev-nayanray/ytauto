"""
Persistent channel settings — stored in channel_settings.json.
Overrides config.py defaults at runtime for niche, seed topics,
pipeline config, and auto-reply behaviour.
"""
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SETTINGS_FILE = Path(__file__).parent / "channel_settings.json"

DEFAULTS: dict[str, Any] = {
    # Channel identity
    "channel_niche":            "AI, technology, and productivity tools",
    "channel_description":      "We create AI & tech tutorials that help people work smarter.",
    "seed_topics": [
        "artificial intelligence", "AI tools", "machine learning tutorial",
        "ChatGPT tips", "Python automation", "AI productivity",
        "how to use AI", "tech explained",
    ],
    # Pipeline config
    "default_count":      1,
    "voice_style":        "professional",   # professional | casual | enthusiastic
    "dry_run_default":    False,
    "publish_delay_hours": 2,
    # Auto-reply config
    "auto_reply_enabled":       True,
    "auto_reply_max_per_video": 5,
    "auto_reply_tone":          "friendly",  # friendly | professional | witty
    "auto_reply_on_upload":     True,
    # Short generation config
    "auto_short_on_upload":     False,
    "short_hook_style":         "question",  # question | statistic | bold-claim
}


def load() -> dict:
    """Load settings, filling missing keys with defaults."""
    if not SETTINGS_FILE.exists():
        return dict(DEFAULTS)
    try:
        saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return {**DEFAULTS, **saved}
    except Exception as exc:
        logger.warning(f"Could not load channel_settings.json: {exc}")
        return dict(DEFAULTS)


def save(settings: dict) -> None:
    """Merge with defaults and persist."""
    merged = {**DEFAULTS, **{k: v for k, v in settings.items() if k in DEFAULTS}}
    SETTINGS_FILE.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    logger.info("Channel settings saved.")


def get(key: str, default: Any = None) -> Any:
    return load().get(key, default)
