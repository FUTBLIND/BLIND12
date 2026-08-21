@echo off
REM ===========================================================================
REM  FUT12 - one-off setup. Needs administrator: it writes the hosts file,
REM  installs a certificate and replaces files inside the game folder.
REM
REM  Everything it does is reversible with UNINSTALL.cmd.
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
    echo   Right-click SETUP.cmd and choose "Run as administrator".
    echo.
    pause
    exit /b 1
)

if not exist "python\python.exe" (
    echo.
    echo   The bundled Python is missing from this folder ^(python\python.exe^).
    echo   The download is not complete.
    echo.
    pause
    exit /b 1
)

"python\python.exe" "setup.py" %*
set RC=%errorlevel%
echo.
pause
exit /b %RC%
