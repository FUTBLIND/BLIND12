@echo off
REM ===========================================================================
REM  FUT12 - put the machine back.
REM
REM  Restores the eleven game files from the backup SETUP.cmd made, removes the
REM  hosts lines it added and drops the certificate it installed. It does not
REM  delete this folder.
REM ===========================================================================
setlocal
cd /d "%~dp0"

REM No .pyc files. Python bytecode embeds the full source path, so a
REM folder that has been run once carries those paths into the next zip.
set PYTHONDONTWRITEBYTECODE=1


net session >nul 2>&1
if not %errorlevel%==0 (
    echo.
    echo   This needs to run as administrator.
    echo.
    pause
    exit /b 1
)

"python\python.exe" "setup.py" --uninstall %*
echo.
pause
