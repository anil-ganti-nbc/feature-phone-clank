@echo off
setlocal enabledelayedexpansion
rem Manual, INTERACTIVE collection run. Unlike run-production.cmd (used by
rem Task Scheduler, output redirected silently to data\scheduled-runs.log
rem only), this script keeps the collector's stdout/stderr visible in the
rem console AS WELL AS logging it, so a fatal error during a manual run is
rem never hidden. Same run lock, same db, same config as the scheduled run
rem - this is just a visible way to trigger it and to confirm collection
rem actually failed or succeeded before you go looking at log files.
cd /d "%~dp0.."

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo ERROR: .venv not found at "%CD%\%PY%" - run: python -m venv .venv ^&^& .venv\Scripts\python.exe -m pip install -e .[dev]
    pause
    exit /b 1
)

if not exist "logs" mkdir "logs"

for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set "STAMP=%%I"
set "LOGFILE=logs\collection-%STAMP%.log"

echo Feature Phone Clank - manual collection run
echo Repo: %CD%
echo Database: %CD%\data\feature_phone_clank.db
echo Log:      %CD%\%LOGFILE%
echo.

powershell -NoProfile -Command "& '%PY%' -m feature_phone_clank.cli run 2>&1 | Tee-Object -FilePath '%LOGFILE%'"
set "EXITCODE=%ERRORLEVEL%"

echo.
if "%EXITCODE%"=="0" (
    echo RESULT: collection command completed ^(exit 0^). Check the JSON above/in
    echo         %LOGFILE% for per-collector status - exit 0 only means the CLI
    echo         itself didn't crash, not that every source reported ok.
) else (
    echo RESULT: collection FAILED ^(exit %EXITCODE%^). See %LOGFILE% and the
    echo         output above for the error.
)
echo.
echo Full log saved to: %CD%\%LOGFILE%
pause
exit /b %EXITCODE%
