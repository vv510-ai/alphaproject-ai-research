@echo off
chcp 65001 >nul
cd /d "%~dp0"
"%~dp0..\AlphaProject\backend\.venv\Scripts\python.exe" -X utf8 -B "%~dp0experiments\02_fetch_spot.py" %*
set "experiment_exit=%errorlevel%"
echo.
pause
exit /b %experiment_exit%
