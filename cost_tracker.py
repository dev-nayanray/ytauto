"""
API cost tracker for ytauto.

Logs every Anthropic API call (tokens + USD) to cost_log.json.
Call log_usage() immediately after each client.messages.create().
"""
import json
import logging
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_BASE = Path(__file__).parent
COST_LOG_FILE = _BASE / "cost_log.json"

# USD per million tokens — update when Anthropic adjusts pricing
MODEL_PRICING: dict[str, dict[str, float]] = {
    "claude-sonnet-4-6":          {"input": 3.00,  "output": 15.00},
    "claude-opus-4-8":            {"input": 15.00, "output": 75.00},
    "claude-haiku-4-5-20251001":  {"input": 0.80,  "output": 4.00},
    "claude-3-5-sonnet-20241022": {"input": 3.00,  "output": 15.00},
    "claude-3-5-haiku-20241022":  {"input": 0.80,  "output": 4.00},
    "claude-3-opus-20240229":     {"input": 15.00, "output": 75.00},
    "default":                    {"input": 3.00,  "output": 15.00},
}


def _load() -> list[dict]:
    if not COST_LOG_FILE.exists():
        return []
    try:
        return json.loads(COST_LOG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save(entries: list[dict]) -> None:
    COST_LOG_FILE.write_text(
        json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def compute_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    pricing = MODEL_PRICING.get(model) or MODEL_PRICING["default"]
    return (input_tokens * pricing["input"] + output_tokens * pricing["output"]) / 1_000_000


def log_usage(
    stage: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    slug: str = "",
    keyword: str = "",
    run_id: str = "",
) -> float:
    """Append one API call entry. Returns cost in USD."""
    cost = compute_cost(model, input_tokens, output_tokens)
    entries = _load()
    entries.append({
        "id":            str(uuid.uuid4()),
        "timestamp":     datetime.utcnow().isoformat(),
        "slug":          slug,
        "keyword":       keyword,
        "stage":         stage,
        "model":         model,
        "input_tokens":  input_tokens,
        "output_tokens": output_tokens,
        "cost_usd":      round(cost, 6),
        "run_id":        run_id,
    })
    _save(entries)
    logger.info(
        f"API cost: {stage} ${cost:.4f} "
        f"({input_tokens:,}in / {output_tokens:,}out) slug={slug or '—'}"
    )
    return cost


def get_all() -> list[dict]:
    return _load()


def get_summary() -> dict:
    entries = _load()
    if not entries:
        return {
            "total_cost_usd":      0.0,
            "total_calls":         0,
            "total_input_tokens":  0,
            "total_output_tokens": 0,
            "this_month_cost_usd": 0.0,
            "this_week_cost_usd":  0.0,
            "avg_cost_per_video":  0.0,
            "by_stage":            {},
            "by_model":            {},
            "unique_videos":       0,
        }

    now          = datetime.utcnow()
    month_prefix = now.strftime("%Y-%m")
    week_cutoff  = (now - timedelta(days=7)).isoformat()

    total_cost   = sum(e["cost_usd"] for e in entries)
    month_cost   = sum(e["cost_usd"] for e in entries if e["timestamp"].startswith(month_prefix))
    week_cost    = sum(e["cost_usd"] for e in entries if e["timestamp"] >= week_cutoff)
    total_input  = sum(e["input_tokens"] for e in entries)
    total_output = sum(e["output_tokens"] for e in entries)

    by_stage: dict[str, float] = {}
    for e in entries:
        by_stage[e["stage"]] = by_stage.get(e["stage"], 0.0) + e["cost_usd"]

    by_model: dict[str, float] = {}
    for e in entries:
        by_model[e["model"]] = by_model.get(e["model"], 0.0) + e["cost_usd"]

    unique_slugs  = len({e["slug"] for e in entries if e["slug"]})
    avg_per_video = total_cost / unique_slugs if unique_slugs else 0.0

    # Cost for each day this month (for mini sparkline)
    daily_this_month: dict[str, float] = {}
    for e in entries:
        if e["timestamp"].startswith(month_prefix):
            day = e["timestamp"][:10]
            daily_this_month[day] = daily_this_month.get(day, 0.0) + e["cost_usd"]

    return {
        "total_cost_usd":      round(total_cost, 4),
        "total_calls":         len(entries),
        "total_input_tokens":  total_input,
        "total_output_tokens": total_output,
        "this_month_cost_usd": round(month_cost, 4),
        "this_week_cost_usd":  round(week_cost, 4),
        "avg_cost_per_video":  round(avg_per_video, 4),
        "by_stage":            {k: round(v, 4) for k, v in by_stage.items()},
        "by_model":            {k: round(v, 4) for k, v in by_model.items()},
        "unique_videos":       unique_slugs,
        "daily_this_month":    {k: round(v, 4) for k, v in sorted(daily_this_month.items())},
    }


def get_per_video() -> list[dict]:
    """Aggregate cost totals per video slug, sorted by cost descending."""
    entries = _load()
    slugs: dict[str, dict[str, Any]] = {}
    for e in entries:
        slug = e["slug"] or "_unknown"
        if slug not in slugs:
            slugs[slug] = {
                "slug":      slug,
                "keyword":   e.get("keyword") or slug.replace("_", " "),
                "total":     0.0,
                "calls":     0,
                "by_stage":  {},
                "latest":    "",
                "estimated": False,
            }
        slugs[slug]["total"]  += e["cost_usd"]
        slugs[slug]["calls"]  += 1
        stage = e["stage"]
        slugs[slug]["by_stage"][stage] = (
            slugs[slug]["by_stage"].get(stage, 0.0) + e["cost_usd"]
        )
        if e["timestamp"] > slugs[slug]["latest"]:
            slugs[slug]["latest"] = e["timestamp"]
        if e.get("estimated"):
            slugs[slug]["estimated"] = True

    result = sorted(slugs.values(), key=lambda x: x["total"], reverse=True)
    for r in result:
        r["total"]    = round(r["total"], 4)
        r["by_stage"] = {k: round(v, 4) for k, v in r["by_stage"].items()}
    return result


def get_daily_totals(days: int = 30) -> list[dict]:
    """Return daily cost totals for last N days, filling missing days with 0."""
    entries = _load()
    cutoff  = (datetime.utcnow() - timedelta(days=days)).date()

    daily: dict[str, float] = {}
    for e in entries:
        try:
            date_str = e["timestamp"][:10]
            if datetime.fromisoformat(date_str).date() >= cutoff:
                daily[date_str] = daily.get(date_str, 0.0) + e["cost_usd"]
        except Exception:
            pass

    result = []
    cur    = cutoff
    today  = datetime.utcnow().date()
    while cur <= today:
        ds = cur.isoformat()
        result.append({"date": ds, "cost_usd": round(daily.get(ds, 0.0), 4)})
        cur += timedelta(days=1)
    return result


# ── Backfill from existing output dirs ────────────────────────────────────────

# Known prompt overhead token counts (constant across all videos)
_SCRIPT_INPUT_OVERHEAD  = 720   # system prompt + user template skeleton (without script body)
_SEO_INPUT_OVERHEAD     = 950   # SEO prompt template + excerpt prefix
_CHARS_PER_TOKEN        = 4.0   # rough average for English text


def backfill_from_outputs(output_dir: Path, model: str = "claude-sonnet-4-6") -> int:
    """
    Estimate and insert cost entries for videos that have no cost log yet.
    Uses file sizes + known prompt overhead to approximate token counts.
    Returns number of new entries inserted.
    """
    existing = _load()
    already_logged: set[str] = set()
    for e in existing:
        if e.get("slug") and not e.get("estimated"):
            already_logged.add(f"{e['slug']}:{e['stage']}")

    new_entries: list[dict] = []

    for d in sorted(output_dir.iterdir()):
        if not d.is_dir():
            continue
        slug = d.name

        # ── Script stage ──────────────────────────────────────────────────────
        script_path = d / "script.txt"
        if script_path.exists() and f"{slug}:script" not in already_logged:
            try:
                script_text  = script_path.read_text(encoding="utf-8")
                output_toks  = max(200, int(len(script_text) / _CHARS_PER_TOKEN))
                input_toks   = _SCRIPT_INPUT_OVERHEAD
                cost         = compute_cost(model, input_toks, output_toks)
                # Use file mtime as approximate timestamp
                mtime = datetime.utcfromtimestamp(script_path.stat().st_mtime).isoformat()
                keyword = slug.replace("_", " ")
                new_entries.append({
                    "id":            str(uuid.uuid4()),
                    "timestamp":     mtime,
                    "slug":          slug,
                    "keyword":       keyword,
                    "stage":         "script",
                    "model":         model,
                    "input_tokens":  input_toks,
                    "output_tokens": output_toks,
                    "cost_usd":      round(cost, 6),
                    "run_id":        "",
                    "estimated":     True,
                })
            except Exception:
                pass

        # ── SEO stage ─────────────────────────────────────────────────────────
        seo_path = d / "seo.json"
        if seo_path.exists() and f"{slug}:seo" not in already_logged:
            try:
                seo_text    = seo_path.read_text(encoding="utf-8")
                output_toks = max(100, int(len(seo_text) / _CHARS_PER_TOKEN))
                # Input includes first 2000 chars of script as context
                excerpt_len = 0
                if script_path.exists():
                    excerpt_len = min(2000, len(script_path.read_text(encoding="utf-8")))
                input_toks  = _SEO_INPUT_OVERHEAD + int(excerpt_len / _CHARS_PER_TOKEN)
                cost        = compute_cost(model, input_toks, output_toks)
                mtime       = datetime.utcfromtimestamp(seo_path.stat().st_mtime).isoformat()
                keyword     = slug.replace("_", " ")
                new_entries.append({
                    "id":            str(uuid.uuid4()),
                    "timestamp":     mtime,
                    "slug":          slug,
                    "keyword":       keyword,
                    "stage":         "seo",
                    "model":         model,
                    "input_tokens":  input_toks,
                    "output_tokens": output_toks,
                    "cost_usd":      round(cost, 6),
                    "run_id":        "",
                    "estimated":     True,
                })
            except Exception:
                pass

    if new_entries:
        existing.extend(new_entries)
        _save(existing)
        logger.info(f"Backfill: inserted {len(new_entries)} estimated cost entries")

    return len(new_entries)
