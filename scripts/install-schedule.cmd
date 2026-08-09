@echo off
setlocal
rem One-time setup: registers a Windows Scheduled Task that runs FEATURE-01
rem silently four times a day (06:30, 12:30, 18:30, 22:30). Run this ONCE
rem (double-click is fine).
cd /d "%~dp0"
set TASK=FeaturePhoneClank HMD Soak

rem Registration itself lives in install-schedule.ps1 (Register-
rem ScheduledTask, Execute/Argument as separate fields) rather than
rem schtasks.exe's "/tr <one combined string>" here - see that script's
rem header comment for why (this project's folder name has a space in it).
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass ^
    -File "%~dp0install-schedule.ps1"

if %errorlevel%==0 (
    echo.
    echo Installed. FEATURE-01 now runs automatically at 06:30, 12:30, 18:30, 22:30 daily.
    echo   - Check status:          status-schedule.cmd
    echo   - Watch the run log:     data\scheduled-runs.log
    echo   - Run one now to test:   schtasks /run /tn "%TASK%"
    echo   - Remove it later:       uninstall-schedule.cmd
) else (
    echo.
    echo Install failed. Try running this file as Administrator
    echo   ^(right-click ^> Run as administrator^).
)
echo.
pause
