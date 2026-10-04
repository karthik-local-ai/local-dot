@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
title Local Dot - voice
python "%~dp0dot.py" --voice %*
if errorlevel 1 pause
