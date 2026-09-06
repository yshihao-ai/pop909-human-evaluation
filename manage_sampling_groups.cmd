@echo off
chcp 65001 >nul
cd /d "%~dp0"
python tools\manage_sampling_groups.py --config evaluation.config.json
if errorlevel 1 pause
