@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
set "SUBTITLE_EXIT=%ERRORLEVEL%"
if not "%REALTIME_SUBTITLE_NO_PAUSE%"=="1" pause
exit /b %SUBTITLE_EXIT%
