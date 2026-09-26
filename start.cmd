@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "release\current\Shijian\Shijian.exe" (
  start "" "release\current\Shijian\Shijian.exe"
  exit /b
)
if exist "release\Shijian\Shijian.exe" (
  start "" "release\Shijian\Shijian.exe"
  exit /b
)
if exist "dist\Shijian\Shijian.exe" (
  start "" "dist\Shijian\Shijian.exe"
  exit /b
)
if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" desktop.py
  exit /b
)
if exist "..\.venv\Scripts\pythonw.exe" (
  start "" "..\.venv\Scripts\pythonw.exe" desktop.py
  exit /b
)
echo 请先安装依赖：python -m pip install -r requirements.txt
python desktop.py
pause
