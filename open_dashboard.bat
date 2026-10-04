@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "URL=http://127.0.0.1:8777/"

if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    python --version >nul 2>nul
    if not errorlevel 1 (
        set "PY=python"
    ) else (
        py -3 --version >nul 2>nul
        if errorlevel 1 goto no_python
        set "PY=py -3"
    )
)

%PY% -c "import yaml" >nul 2>nul
if errorlevel 1 goto missing_deps

call :ready
if not errorlevel 1 goto open

echo 正在启动管理台...
start "bot-mindscape 管理台" /min cmd /k "%PY% scripts\web_ui.py"
for /l %%I in (1,1,30) do (
    call :ready
    if not errorlevel 1 goto open
    timeout /t 1 /nobreak >nul
)
echo 管理台未能启动，请查看任务栏中的管理台窗口。
pause
exit /b 1

:open
start "" "%URL%"
exit /b 0

:ready
%PY% -c "import http.client,sys; c=http.client.HTTPConnection('127.0.0.1',8777,timeout=1); c.request('GET','/'); r=c.getresponse(); sys.exit(0 if r.status==200 and b'bot-mindscape' in r.read(4096) else 1)" >nul 2>nul
exit /b

:no_python
echo 未找到 Python 3，请先安装 Python，再双击此文件。
pause
exit /b 1

:missing_deps
echo 缺少 PyYAML，请先运行：%PY% -m pip install -r requirements.txt
pause
exit /b 1
