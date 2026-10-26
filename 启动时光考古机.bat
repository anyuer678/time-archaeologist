@echo off
rem 时光考古机 Time Archaeologist 双击启动器
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 Python，请先安装 Python 3.12 并勾选 "Add to PATH"。
    pause
    exit /b 1
)
python run_app.py
if errorlevel 1 pause
