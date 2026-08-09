@echo off
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass ^
    -File "%~dp0status-schedule.ps1"
pause
