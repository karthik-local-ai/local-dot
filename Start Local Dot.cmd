@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
title Local Dot - text
python "%~dp0dot.py" %*
if errorlevel 1 pause
