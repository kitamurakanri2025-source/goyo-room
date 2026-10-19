@echo off
chcp 65001 > nul
cd /d %~dp0
python raconne_jobs.py --reset --pages 2
pause
