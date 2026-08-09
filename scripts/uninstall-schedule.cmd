@echo off
set TASK=FeaturePhoneClank HMD Soak
schtasks /delete /tn "%TASK%" /f
echo Removed the FEATURE-01 scheduled task (if it existed). You can still run
echo it manually any time: .venv\Scripts\python.exe -m feature_phone_clank.cli run
pause
