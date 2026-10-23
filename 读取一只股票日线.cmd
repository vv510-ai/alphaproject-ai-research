@echo off
chcp 65001 >nul
cd /d "%~dp0"
"%~dp0..\AlphaProject\backend\.venv\Scripts\python.exe" -X utf8 -B "%~dp0experiments\01_fetch_daily.py" %*
set "experiment_exit=%errorlevel%"
echo.
pause
exit /b %experiment_exit%
