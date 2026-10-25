@echo off
title 大肥鱼控制中心
cd /d "%~dp0"
set "USERPROFILE=%~dp0..\ollama-home"
set "OLLAMA_MODELS=%~dp0..\models"
set "OLLAMA_KEEP_ALIVE=8h"
echo ============================================
echo   大肥鱼控制中心
echo ============================================
echo.
echo 启动控制台...
start "" http://127.0.0.1:8080
python app.py
pause