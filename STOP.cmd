@echo off
REM  FUT12 - stop the watchdog and all seven services. Leaves the game alone.
setlocal
cd /d "%~dp0"

REM No .pyc files. Python bytecode embeds the full source path, so a
REM folder that has been run once carries those paths into the next zip.
set PYTHONDONTWRITEBYTECODE=1

powershell -NoProfile -ExecutionPolicy Bypass -File "server\launcher\stop.ps1" %*
echo.
pause
