"""
Orchestrates the full YouTube automation pipeline.

Usage
-----
  python master.py                  # 1 video (recommended default for ramp-up)
  python master.py --count 3        # produce 3 videos this run
  python master.py --dry-run        # all stages except the actual YouTube upload
  python master.py --count 5 --dry-run

Stages per keyword
------------------
  research -> script -> voice -> visuals -> assemble -> seo -> thumb -> upload

Each stage is idempotent: if its output file already exists, the stage is
skipped.  A keyword that fails mid-pipeline is logged and skipped; the
remaining keywords continue.
"""
import argparse
import logging
import re
import sys
from pathlib import Path

# Bootstrap logging before importing other project modules
from config import MAX_UPLOADS_PER_DAY, OUTPUT_DIR, setup_logging

setup_logging()
logger = logging.getLogger(__name__)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _slugify(text: str) -> str:
    """Convert a keyword to a filesystem-safe directory name."""
    text = re.sub(r"[^\w\s-]", "", text.lower())
    text = re.sub(r"[\s]+", "_", text.strip())
    return text[:60]


# ── Single-keyword pipeline ────────────────────────────────────────────────────

def _run_keyword(
    keyword: str,
    video_index: int,
    dry_run: bool,
    publish_times: list,
) -> bool:
    """
    Run every pipeline stage for *keyword*.
    Returns True on success, False if the keyword must be skipped.
    """
    # Lazy imports so logging is already configured when modules load
    from script import generate_script
    from voice import generate_voice
    from visuals import download_clips
    from assemble import assemble_video
    from seo import generate_seo
    from thumb import create_thumbnail
    from upload import upload_video
    from telegram_bot import (
        notify_stage, notify_video_complete, notify_error,
    )
    from sheets import sync_video
    from channel_optimizer import run_full_audit

    slug = _slugify(keyword)
    out_dir = OUTPUT_DIR / slug
    out_dir.mkdir(parents=True, exist_ok=True)

    sep = "=" * 60
    logger.info(f"\n{sep}")
    logger.info(f"  VIDEO {video_index + 1}: {keyword}")
    logger.info(f"  Dir : {out_dir}")
    logger.info(sep)

    try:
        # research stage already done in main() — notify start of this keyword
        notify_stage(keyword, "research", 1)

        # ── 1. Script ──────────────────────────────────────────────────────────
        script_path = out_dir / "script.txt"
        generate_script(keyword, script_path)
        notify_stage(keyword, "script", 2)

        # ── 2. Voice ───────────────────────────────────────────────────────────
        voice_path = out_dir / "voice.mp3"
        generate_voice(script_path, voice_path)
        notify_stage(keyword, "voice", 3)

        # ── 3. Visuals (Pexels clips) ──────────────────────────────────────────
        clips_dir = out_dir / "clips"
        clips = download_clips(keyword, clips_dir)
        if not clips:
            logger.error(f"No Pexels clips available for '{keyword}'. Skipping keyword.")
            return False
        notify_stage(keyword, "visuals", 4)

        # ── 4. Assemble ────────────────────────────────────────────────────────
        video_path = out_dir / "video.mp4"
        work_dir = out_dir / "work"
        assemble_video(clips, voice_path, video_path, work_dir)
        notify_stage(keyword, "assemble", 5)

        # ── 5. SEO metadata ────────────────────────────────────────────────────
        seo_path = out_dir / "seo.json"
        seo = generate_seo(keyword, script_path, seo_path)
        notify_stage(keyword, "seo", 6)

        # ── 6. Thumbnail ───────────────────────────────────────────────────────
        thumb_path = out_dir / "thumbnail.jpg"
        create_thumbnail(video_path, seo["title"], thumb_path)
        notify_stage(keyword, "thumbnail", 7)

        # ── 7. Upload (skipped in dry-run) ─────────────────────────────────────
        video_id = None
        if dry_run:
            logger.info("[DRY-RUN] Skipping YouTube upload.")
        else:
            if video_index >= len(publish_times):
                logger.warning("Publish schedule exhausted — skipping upload.")
                return True
            video_id = upload_video(
                video_path=video_path,
                thumbnail_path=thumb_path,
                seo=seo,
                publish_at=publish_times[video_index],
                output_dir=out_dir,
            )
            notify_video_complete(keyword, seo.get("title", keyword), video_id,
                                  thumbnail_path=thumb_path)

        # ── 8. Google Drive full backup + Sheets sync + rank audit ─────────────
        try:
            from gdrive import backup_full_folder
            from telegram_bot import notify_drive_backup
            folder_url = backup_full_folder(out_dir, keyword)
            notify_drive_backup(keyword, folder_url)
            # Sync to Google Sheets + run SEO audit
            audit = run_full_audit(out_dir, keyword, video_id if not dry_run else None)
            sync_video(keyword, out_dir, drive_folder_url=folder_url, rank=audit.get("rank"))
        except Exception as exc:
            logger.warning(f"Drive/Sheets/rank failed: {exc}")

        return True

    except Exception as exc:
        logger.error(
            f"Pipeline failed for '{keyword}': {exc}",
            exc_info=True,
        )
        notify_error(keyword, "pipeline", str(exc))
        return False


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Faceless YouTube automation pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--count",
        type=int,
        default=1,
        metavar="N",
        help=(
            "Number of videos to produce this run (default: 1). "
            "Recommended: keep at 1 for the first two weeks to let the channel "
            "warm up before YouTube's algorithm starts distributing content. "
            "Capped at 1 to 5 (MAX_UPLOADS_PER_DAY enforced separately)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run all stages except the YouTube upload.",
    )
    args = parser.parse_args()

    count = min(args.count, MAX_UPLOADS_PER_DAY, 5)
    dry_run: bool = args.dry_run

    logger.info(f"Master pipeline starting  count={count}  dry_run={dry_run}")

    # ── Keyword selection: queue first, then research ─────────────────────────
    try:
        from keywords_queue import load_queue, pop_keywords

        queued = load_queue()
        if len(queued) >= count:
            # Enough queued keywords — no research needed this run
            keywords = pop_keywords(count)
            logger.info(f"Using {count} keyword(s) from queue: {keywords}")
        else:
            # Pop whatever's in the queue, fill rest with research
            from_queue = pop_keywords(len(queued))
            needed     = count - len(from_queue)
            from research import get_trending_keywords
            researched = get_trending_keywords(count=max(5, needed * 2))
            keywords   = from_queue + researched[:needed]
            if from_queue:
                logger.info(f"Queue supplied: {from_queue}; research supplied: {researched[:needed]}")
            else:
                logger.info(f"Queue empty — using research: {keywords}")
    except Exception as exc:
        logger.critical(f"Keyword selection failed — cannot continue: {exc}", exc_info=True)
        sys.exit(1)

    # ── Publish schedule ───────────────────────────────────────────────────────
    publish_times = []
    if not dry_run:
        from upload import get_publish_schedule

        publish_times = get_publish_schedule(count)
        for i, pt in enumerate(publish_times):
            logger.info(f"  Video {i + 1} scheduled for {pt.strftime('%Y-%m-%d %H:%M UTC')}")

    # ── Notify pipeline start ──────────────────────────────────────────────────
    try:
        from telegram_bot import notify_pipeline_start
        notify_pipeline_start(count)
    except Exception:
        pass

    # ── Per-keyword pipeline ───────────────────────────────────────────────────
    successes = 0
    for i, kw in enumerate(keywords[:count]):
        ok = _run_keyword(kw, i, dry_run, publish_times)
        if ok:
            successes += 1

    logger.info(f"\nPipeline complete: {successes}/{count} video(s) succeeded.")
    if successes < count:
        sys.exit(1)  # non-zero exit lets run.bat / Task Scheduler detect failures


if __name__ == "__main__":
    main()
