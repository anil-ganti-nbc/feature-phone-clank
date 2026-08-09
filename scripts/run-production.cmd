@echo off
setlocal
rem Headless production run - invoked by Task Scheduler via run-silent.vbs
rem (or run manually to test/soak-check). One-shot: no daemon, no lingering
rem window. Uses the project's own .venv, the production DB/config, and the
rem run lock (never --no-lock) - see scripts/README.md.
cd /d "%~dp0.."

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [%date% %time%] ERROR: .venv not found at "%CD%\%PY%" - run: python -m venv .venv ^&^& .venv\Scripts\python.exe -m pip install -e .[dev] >> data\scheduled-runs.log
    exit /b 1
)

echo [%date% %time%] scheduled run start >> data\scheduled-runs.log
"%PY%" -m feature_phone_clank.cli run >> data\scheduled-runs.log 2>&1
set EXITCODE=%errorlevel%
echo [%date% %time%] scheduled run done (exit %EXITCODE%) >> data\scheduled-runs.log
exit /b %EXITCODE%
