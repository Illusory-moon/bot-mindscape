@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

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

for /l %%P in (8777,1,8787) do (
    call :ready %%P
    if not errorlevel 1 (
        set "PORT=%%P"
        goto open
    )
)

for /l %%P in (8777,1,8787) do (
    call :free %%P
    if not errorlevel 1 (
        set "PORT=%%P"
        goto launch
    )
)
echo 8777-8787 端口都已占用，请关闭旧管理台后重试。
pause
exit /b 1

:launch
echo 正在启动管理台...
start "bot-mindscape 管理台" /min cmd /k "%PY% scripts\web_ui.py --port %PORT%"
for /l %%I in (1,1,30) do (
    call :ready %PORT%
    if not errorlevel 1 goto open
    timeout /t 1 /nobreak >nul
)
echo 管理台未能启动，请查看任务栏中的管理台窗口。
pause
exit /b 1

:open
start "" "http://127.0.0.1:%PORT%/"
exit /b 0

:ready
%PY% -c "import http.client,sys; c=http.client.HTTPConnection('127.0.0.1',%~1,timeout=1); c.request('GET','/'); r=c.getresponse(); body=r.read(); sys.exit(0 if r.status==200 and b'bot-mindscape' in body and b'impression-files' in body else 1)" >nul 2>nul
exit /b

:free
%PY% -c "import socket,sys; s=socket.socket(); s.settimeout(1); result=s.connect_ex(('127.0.0.1',%~1)); s.close(); sys.exit(0 if result else 1)" >nul 2>nul
exit /b

:no_python
echo 未找到 Python 3，请先安装 Python，再双击此文件。
pause
exit /b 1

:missing_deps
echo 缺少 PyYAML，请先运行：%PY% -m pip install -r requirements.txt
pause
exit /b 1
