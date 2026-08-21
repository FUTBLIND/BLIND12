@echo off
REM ===========================================================================
REM  FUT12 - start the local server and launch FIFA 12.
REM
REM  Run SETUP.cmd once, as administrator, before the first launch.
REM
REM  This needs administrator too: the game itself runs elevated, and the
REM  launcher stops the EA App while the game is up and puts it back after.
REM ===========================================================================
setlocal
cd /d "%~dp0"

REM No .pyc files. Python bytecode embeds the full source path, so a
REM folder that has been run once carries those paths into the next zip.
set PYTHONDONTWRITEBYTECODE=1


if not exist "python\python.exe" (
    echo.
    echo   The bundled Python is missing from this folder ^(python\python.exe^).
    echo.
    pause
    exit /b 1
)

if not exist "server\gamepath.txt" (
    echo.
    echo   Not set up yet - run SETUP.cmd as administrator first.
    echo.
    pause
    exit /b 1
)

REM boot.ps1 does its own pre-flight, starts the seven services, arms the
REM watchdog and then hands over to launch_fifa.ps1, which self-elevates.
powershell -NoProfile -ExecutionPolicy Bypass -File "server\launcher\boot.ps1" %*
set RC=%errorlevel%
if not %RC%==0 (
    echo.
    echo   Boot reported a problem - the detail is above.
    echo.
    pause
)
exit /b %RC%
