"""
Goal tracking system for ytauto.

Goals have a type, a numeric target, an optional deadline, and a status.
Call check_all_goals(stats) after every pipeline run or analytics refresh —
it returns newly-completed goals so callers can fire notifications.
"""
import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_BASE      = Path(__file__).parent
GOALS_FILE = _BASE / "goals.json"

# ── Goal type catalogue ────────────────────────────────────────────────────────

GOAL_TYPES: dict[str, dict] = {
    "upload_count":    {"label": "Videos Published",    "unit": "videos",  "icon": "🎬", "stat_key": "upload_count"},
    "subscriber":      {"label": "Subscribers",          "unit": "subs",    "icon": "👥", "stat_key": "subscribers"},
    "view_count":      {"label": "Total Views",          "unit": "views",   "icon": "👁",  "stat_key": "total_views"},
    "watch_hours":     {"label": "Watch Hours",          "unit": "hours",   "icon": "⏱",  "stat_key": "watch_hours"},
    "ypp":             {"label": "YPP Ready",            "unit": "%",       "icon": "💰", "stat_key": "ypp_pct"},
    "monthly_uploads": {"label": "Uploads This Month",   "unit": "videos",  "icon": "📅", "stat_key": "monthly_uploads"},
    "monthly_views":   {"label": "Views This Month",     "unit": "views",   "icon": "📈", "stat_key": "monthly_views"},
    "cost_limit":      {"label": "API Cost Under",       "unit": "USD",     "icon": "💵", "stat_key": "monthly_api_cost"},
    "likes_total":     {"label": "Total Likes",          "unit": "likes",   "icon": "👍", "stat_key": "total_likes"},
    "comments_total":  {"label": "Total Comments",       "unit": "comments","icon": "💬", "stat_key": "total_comments"},
}

STATUS_ACTIVE    = "active"
STATUS_COMPLETED = "completed"
STATUS_FAILED    = "failed"
STATUS_PAUSED    = "paused"


# ── Persistence ───────────────────────────────────────────────────────────────

def _load() -> list[dict]:
    if not GOALS_FILE.exists():
        return []
    try:
        return json.loads(GOALS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save(goals: list[dict]) -> None:
    GOALS_FILE.write_text(json.dumps(goals, indent=2, ensure_ascii=False), encoding="utf-8")


# ── CRUD ─────────────────────────────────────────────────────────────────────

def create_goal(
    goal_type: str,
    target: float,
    title: str = "",
    deadline: str = "",   # ISO date "YYYY-MM-DD" or ""
    notes: str = "",
) -> dict:
    if goal_type not in GOAL_TYPES:
        raise ValueError(f"Unknown goal type: {goal_type}")
    meta = GOAL_TYPES[goal_type]
    goal: dict[str, Any] = {
        "id":          str(uuid.uuid4()),
        "type":        goal_type,
        "title":       title or f"Reach {int(target):,} {meta['unit']}",
        "target":      target,
        "current":     0.0,
        "unit":        meta["unit"],
        "icon":        meta["icon"],
        "deadline":    deadline,
        "notes":       notes,
        "status":      STATUS_ACTIVE,
        "created_at":  datetime.utcnow().isoformat(),
        "completed_at": None,
        # for cost_limit: goal is complete when current STAYS UNDER target
        "invert":      goal_type == "cost_limit",
    }
    goals = _load()
    goals.append(goal)
    _save(goals)
    logger.info(f"Goal created: {goal['title']} (type={goal_type}, target={target})")
    return goal


def load_goals() -> list[dict]:
    return _load()


def get_goal(goal_id: str) -> dict | None:
    return next((g for g in _load() if g["id"] == goal_id), None)


def delete_goal(goal_id: str) -> bool:
    goals = _load()
    before = len(goals)
    goals = [g for g in goals if g["id"] != goal_id]
    if len(goals) < before:
        _save(goals)
        return True
    return False


def update_goal(goal_id: str, **kwargs) -> dict | None:
    goals = _load()
    for g in goals:
        if g["id"] == goal_id:
            allowed = {"title", "target", "deadline", "notes", "status"}
            for k, v in kwargs.items():
                if k in allowed:
                    g[k] = v
            _save(goals)
            return g
    return None


# ── Progress check ────────────────────────────────────────────────────────────

def check_all_goals(stats: dict) -> list[dict]:
    """
    Update every active goal's current value from *stats* dict.
    Returns list of goals that NEWLY completed this call.
    stats keys (all optional):
        upload_count, subscribers, total_views, watch_hours,
        ypp_pct, monthly_uploads, monthly_views, monthly_api_cost,
        total_likes, total_comments
    """
    goals    = _load()
    newly_completed: list[dict] = []
    changed  = False
    now_iso  = datetime.utcnow().isoformat()

    for g in goals:
        if g["status"] != STATUS_ACTIVE:
            # Check deadline failures
            if g.get("deadline") and g["status"] == STATUS_ACTIVE:
                if datetime.utcnow().date().isoformat() > g["deadline"]:
                    g["status"] = STATUS_FAILED
                    changed = True
            continue

        stat_key = GOAL_TYPES.get(g["type"], {}).get("stat_key")
        if not stat_key or stat_key not in stats:
            continue

        current = float(stats[stat_key] or 0)
        g["current"] = current

        # Check completion
        invert = g.get("invert", False)  # cost_limit: complete when current < target
        if invert:
            completed = (current <= g["target"])
        else:
            completed = (current >= g["target"])

        if completed and g["status"] == STATUS_ACTIVE:
            g["status"]       = STATUS_COMPLETED
            g["completed_at"] = now_iso
            newly_completed.append(dict(g))
            logger.info(f"Goal completed: {g['title']} ({current} / {g['target']})")

        # Check deadline failure
        if g.get("deadline") and datetime.utcnow().date().isoformat() > g["deadline"]:
            if g["status"] == STATUS_ACTIVE:
                g["status"] = STATUS_FAILED
                changed = True

        changed = True

    if changed:
        _save(goals)

    return newly_completed


def get_summary() -> dict:
    goals   = _load()
    active  = [g for g in goals if g["status"] == STATUS_ACTIVE]
    done    = [g for g in goals if g["status"] == STATUS_COMPLETED]
    failed  = [g for g in goals if g["status"] == STATUS_FAILED]
    return {
        "total":     len(goals),
        "active":    len(active),
        "completed": len(done),
        "failed":    len(failed),
        "goals":     goals,
    }


def seed_default_goals() -> int:
    """Add starter goals if none exist yet."""
    if _load():
        return 0
    defaults = [
        ("subscriber",   1000,  "Reach 1,000 Subscribers (YPP)", ""),
        ("watch_hours",  4000,  "Reach 4,000 Watch Hours (YPP)", ""),
        ("upload_count", 50,    "Publish 50 Videos",              ""),
        ("view_count",   10000, "Reach 10,000 Total Views",       ""),
        ("ypp",          100,   "Complete YPP Requirements",       ""),
        ("monthly_uploads", 8,  "Upload 8 Videos This Month",     ""),
    ]
    for gtype, target, title, deadline in defaults:
        create_goal(gtype, target, title=title, deadline=deadline)
    return len(defaults)


# ── Smart goal milestones ─────────────────────────────────────────────────────

def _next_subscriber_milestone(current: int) -> int:
    milestones = [100, 250, 500, 1000, 2500, 5000, 10000, 25000, 50000, 100000, 500000, 1000000]
    return next((m for m in milestones if m > current), current * 2)


def _next_views_milestone(current: int) -> int:
    milestones = [500, 1000, 2500, 5000, 10000, 25000, 50000, 100000, 500000, 1000000]
    return next((m for m in milestones if m > current), current * 2)


def _next_watch_hours_milestone(current: float) -> float:
    milestones = [100, 250, 500, 1000, 2000, 4000, 10000, 25000]
    return next((m for m in milestones if m > current), current * 2)


def _next_upload_milestone(current: int) -> int:
    milestones = [10, 20, 30, 50, 75, 100, 150, 200, 300, 500]
    return next((m for m in milestones if m > current), current + 50)


def _smart_monthly_upload_target(current_monthly: int) -> int:
    """Suggest a +25-50% stretch over current pace, snapped to clean number."""
    if current_monthly <= 1:
        return 4
    if current_monthly <= 3:
        return 6
    if current_monthly <= 5:
        return 8
    if current_monthly <= 8:
        return 10
    return current_monthly + 4


def _active_types() -> set[str]:
    """Return goal types that already have an active goal."""
    return {g["type"] for g in _load() if g["status"] == STATUS_ACTIVE}


def analyze_and_generate_goals(stats: dict, insights: dict | None = None) -> dict:
    """
    Analyze current channel stats + agent memory insights and create
    smart, personalized goals based on real channel state.

    stats keys accepted:
        subscribers, total_views, watch_hours, upload_count,
        monthly_uploads, monthly_views, total_likes, ypp_pct,
        avg_views_per_video, engagement_rate, ctr

    Returns:
        {
          "created": [list of created goal dicts],
          "skipped": [list of {type, reason} skipped],
          "analysis": str   — human-readable channel analysis
        }
    """
    ins        = insights or {}
    active     = _active_types()
    created    = []
    skipped    = []
    analysis   = []

    subs        = int(stats.get("subscribers", 0))
    total_views = int(stats.get("total_views", 0))
    watch_h     = float(stats.get("watch_hours", 0))
    uploads     = int(stats.get("upload_count", 0))
    mo_uploads  = int(stats.get("monthly_uploads", 0))
    mo_views    = int(stats.get("monthly_views", 0))
    avg_views   = int(ins.get("avg_views", 0) or stats.get("avg_views_per_video", 0))
    eng_rate    = float(ins.get("avg_engagement", 0) or stats.get("engagement_rate", 0))
    ctr         = float(stats.get("ctr", 0))
    ypp_pct     = float(stats.get("ypp_pct", 0))

    # ── Channel health summary ────────────────────────────────────────────────
    if subs < 100:
        analysis.append(f"📊 Early-stage channel with {subs:,} subscribers — focus on consistency and topic niche.")
    elif subs < 1000:
        analysis.append(f"📊 Growing channel with {subs:,} subscribers — YPP subscriber goal is in reach.")
    else:
        analysis.append(f"📊 Established channel with {subs:,} subscribers.")

    if watch_h < 4000:
        pct = round(watch_h / 4000 * 100)
        analysis.append(f"⏱ Watch hours: {watch_h:,.1f} / 4,000 ({pct}% to YPP).")
    else:
        analysis.append(f"⏱ Watch hours: {watch_h:,.1f} — YPP threshold met ✅")

    if avg_views:
        analysis.append(f"👁 Average views per video: {avg_views:,}.")
    if eng_rate:
        rating = "strong" if eng_rate >= 0.04 else "average" if eng_rate >= 0.01 else "low"
        analysis.append(f"💬 Engagement rate: {eng_rate:.1%} ({rating}).")
    if ctr:
        ctr_rating = "excellent" if ctr >= 0.06 else "good" if ctr >= 0.03 else "needs work"
        analysis.append(f"🖱 Click-through rate: {ctr:.1%} ({ctr_rating}).")

    best_day = ins.get("best_upload_day")
    if best_day:
        analysis.append(f"📅 Best performing upload day based on your history: {best_day}.")

    # ── Subscriber goal ───────────────────────────────────────────────────────
    if "subscriber" not in active:
        target = _next_subscriber_milestone(subs)
        if target > subs:
            pct_done = round(subs / target * 100)
            g = create_goal("subscriber", target,
                            title=f"Reach {target:,} Subscribers",
                            notes=f"Currently at {subs:,} subs ({pct_done}% done). Agent-generated.")
            created.append(g)
            analysis.append(f"🎯 Created subscriber goal: {subs:,} → {target:,}.")
    else:
        skipped.append({"type": "subscriber", "reason": "Active goal already exists"})

    # ── Watch hours goal ──────────────────────────────────────────────────────
    if "watch_hours" not in active:
        target_wh = _next_watch_hours_milestone(watch_h)
        label = " (YPP threshold!)" if target_wh == 4000 else ""
        g = create_goal("watch_hours", target_wh,
                        title=f"Reach {target_wh:,.0f} Watch Hours{label}",
                        notes=f"Currently at {watch_h:.1f}h. Agent-generated.")
        created.append(g)
        analysis.append(f"🎯 Created watch hours goal: {watch_h:.0f}h → {target_wh:.0f}h.")
    else:
        skipped.append({"type": "watch_hours", "reason": "Active goal already exists"})

    # ── Total views goal ──────────────────────────────────────────────────────
    if "view_count" not in active:
        target_v = _next_views_milestone(total_views)
        g = create_goal("view_count", target_v,
                        title=f"Reach {target_v:,} Total Views",
                        notes=f"Currently at {total_views:,} views. Agent-generated.")
        created.append(g)
        analysis.append(f"🎯 Created total views goal: {total_views:,} → {target_v:,}.")
    else:
        skipped.append({"type": "view_count", "reason": "Active goal already exists"})

    # ── Upload count goal ─────────────────────────────────────────────────────
    if "upload_count" not in active:
        target_u = _next_upload_milestone(uploads)
        g = create_goal("upload_count", target_u,
                        title=f"Publish {target_u} Videos",
                        notes=f"Currently at {uploads} videos published. Agent-generated.")
        created.append(g)
        analysis.append(f"🎯 Created upload count goal: {uploads} → {target_u} videos.")
    else:
        skipped.append({"type": "upload_count", "reason": "Active goal already exists"})

    # ── Monthly uploads cadence goal ──────────────────────────────────────────
    if "monthly_uploads" not in active:
        target_mo = _smart_monthly_upload_target(mo_uploads)
        g = create_goal("monthly_uploads", target_mo,
                        title=f"Upload {target_mo} Videos This Month",
                        notes=f"Last month: {mo_uploads} uploads. Agent-generated.")
        created.append(g)
        analysis.append(f"🎯 Created monthly cadence goal: target {target_mo} uploads/month.")
    else:
        skipped.append({"type": "monthly_uploads", "reason": "Active goal already exists"})

    # ── Monthly views stretch goal (only if channel has some traction) ────────
    if "monthly_views" not in active and mo_views > 100:
        target_mv = max(mo_views * 2, 1000)
        g = create_goal("monthly_views", target_mv,
                        title=f"Double Monthly Views to {target_mv:,}",
                        notes=f"Current monthly views: {mo_views:,}. Agent-generated.")
        created.append(g)
        analysis.append(f"🎯 Created monthly views goal: {mo_views:,} → {target_mv:,}.")

    # ── YPP completion goal ───────────────────────────────────────────────────
    if "ypp" not in active and ypp_pct < 100:
        g = create_goal("ypp", 100,
                        title="Complete YouTube Partner Program Requirements",
                        notes=f"Currently {ypp_pct:.0f}% complete. Agent-generated.")
        created.append(g)
        analysis.append(f"🎯 Created YPP goal ({ypp_pct:.0f}% complete).")
    elif "ypp" not in active and ypp_pct >= 100:
        skipped.append({"type": "ypp", "reason": "YPP requirements already met"})

    logger.info(f"Agent analysis: created {len(created)} goals, skipped {len(skipped)}")
    return {
        "created":  created,
        "skipped":  skipped,
        "analysis": " ".join(analysis),
        "analysis_lines": analysis,
    }
