"""
Daily pipeline scheduler — runs master.py once per day at a configurable time.

Usage:
    python scheduler.py            # runs daily at 09:00 (default)
    python scheduler.py --time 14:30
    python scheduler.py --now      # run immediately then schedule

Keep this running in a terminal or add it to Windows Startup.
"""
import argparse
import logging
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [SCHEDULER] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(str(Path(__file__).parent / "scheduler.log"), encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

BASE   = Path(__file__).parent
MASTER = BASE / "master.py"
PYTHON = sys.executable


def run_pipeline(count: int = 1) -> None:
    logger.info(f"Starting pipeline (count={count})")
    result = subprocess.run(
        [PYTHON, str(MASTER), "--count", str(count)],
        cwd=str(BASE),
    )
    if result.returncode == 0:
        logger.info("Pipeline completed successfully")
    else:
        logger.error(f"Pipeline exited with code {result.returncode}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--time",  default="09:00", help="Daily run time HH:MM (default 09:00)")
    parser.add_argument("--count", type=int, default=1, help="Videos per run (default 1)")
    parser.add_argument("--now",   action="store_true", help="Run immediately then schedule")
    args = parser.parse_args()

    logger.info(f"Scheduler started — daily run at {args.time}, count={args.count}")

    if args.now:
        run_pipeline(args.count)

    while True:
        now = datetime.now().strftime("%H:%M")
        if now == args.time:
            run_pipeline(args.count)
            time.sleep(61)   # prevent double-trigger within the same minute
        time.sleep(30)


if __name__ == "__main__":
    main()
