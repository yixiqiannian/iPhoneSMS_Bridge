@echo off
setlocal
cd /d "%~dp0"
title iPhoneSMS_Bridge

set "PY=venv\Scripts\python.exe"
set "PYW=venv\Scripts\pythonw.exe"

rem ============ 1. 运行环境（venv）检查 ============
if not exist "%PY%" (
    echo.
    echo [1/3] 首次运行，正在创建运行环境，大约 1-3 分钟，请耐心等待...
    echo.
    where py >nul 2>nul
    if not errorlevel 1 (
        py -3 -m venv venv
    ) else (
        python -m venv venv
    )
    if not exist "%PY%" (
        echo.
        echo [错误] 创建运行环境失败：这台电脑似乎没有安装 Python 3。
        echo        请先访问 https://www.python.org/downloads/ 下载安装，
        echo        安装时务必勾选 "Add python.exe to PATH"，然后重新双击本文件。
        echo.
        pause
        exit /b 1
    )
)

rem ============ 2. 依赖检查 ============
"%PY%" -c "import flask, winotify, webview" >nul 2>nul
if errorlevel 1 (
    echo [2/3] 正在安装依赖，需要联网，大约 1-2 分钟...
    "%PY%" -m pip install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple >nul 2>nul
    "%PY%" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    "%PY%" -m pip install pywebview -i https://pypi.tuna.tsinghua.edu.cn/simple
    "%PY%" -c "import flask, winotify, webview" >nul 2>nul
    if errorlevel 1 (
        echo.
        echo [错误] 依赖安装失败，请检查网络后重新双击本文件。
        echo        若反复失败，可先用手机热点或换个镜像源再试。
        echo.
        pause
        exit /b 1
    )
)

rem ============ 3. 启动主程序 ============
echo [3/3] 正在启动 iPhoneSMS_Bridge 窗口...
if exist "%PYW%" (
    start "" "%PYW%" app.py
) else (
    start "" "%PY%" app.py
)
exit /b 0
