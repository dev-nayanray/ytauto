@echo off
:: ──────────────────────────────────────────────────────────────────────────────
:: run.bat  — daily YouTube automation runner
:: Pin this script in Windows Task Scheduler to run once per day.
:: Output (stdout + stderr) is appended to log.txt in the same directory.
:: ──────────────────────────────────────────────────────────────────────────────

cd /d "%~dp0"

echo. >> log.txt
echo ============================================================ >> log.txt
echo [%date% %time%]  ytauto run starting >> log.txt
echo ============================================================ >> log.txt

:: Default: produce 1 video per run (ramp-up mode for new channels).
:: After the first 2 weeks change --count to 3 or 5.
python master.py --count 1 >> log.txt 2>&1

if %errorlevel% equ 0 (
    echo [%date% %time%]  Run SUCCEEDED >> log.txt
) else (
    echo [%date% %time%]  Run FAILED  ^(exit code %errorlevel%^) >> log.txt
)

echo. >> log.txt
