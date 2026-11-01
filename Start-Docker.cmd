@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\start-docker.ps1" %*
set "START_EXIT=%ERRORLEVEL%"
if not "%START_EXIT%"=="0" echo Deployment failed. Please read the message above.
pause
exit /b %START_EXIT%
