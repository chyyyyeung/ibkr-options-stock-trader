@echo off
chcp 65001 >nul
title ibkr_trader — 环境配置
cd /d "%~dp0"

echo ============================================================
echo   IBKR 点价交易 GUI 一次性环境配置
echo   需要: Python 3.13+ 已装并在 PATH 里
echo ============================================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo [X] 找不到 python — 先装 Python 3.13 并勾选 "Add to PATH"。
    pause
    exit /b 1
)

REM 光"找得到 python"不够: PATH 上第一个 python 可能是别的发行版。
REM (实例: PATH 中排在前面的其他 Python 发行版版本较旧且没有 pip。
REM  它能通过上面的 --version 检查, 然后 pip 才报 "No module named pip",
REM  报错指向完全错误的方向。) 所以这里验版本 >= 3.13 且带 pip。
for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
for /f "tokens=1,2 delims=." %%a in ("%PYVER%") do (
    set PYMAJ=%%a
    set PYMIN=%%b
)
for /f "delims=" %%p in ('python -c "import sys;print(sys.executable)" 2^>nul') do set PYEXE=%%p
if %PYMAJ% LSS 3 goto :badpy
if %PYMAJ% EQU 3 if %PYMIN% LSS 13 goto :badpy
python -m pip --version >nul 2>&1
if errorlevel 1 goto :nopip
echo     python OK: %PYEXE% (%PYVER%)

echo [1/1] 安装依赖 (ibapi / PyQt5 / numpy / pyqtgraph / psutil) ...
python -m pip install -r requirements.txt -q
if errorlevel 1 goto :err

echo.
echo ============================================================
echo   完成。运行前还需在 TWS 里做两件事 (一次性):
echo.
echo   1. Global Configuration -^> API -^> Settings:
echo      勾选 "Enable ActiveX and Socket Clients", 端口 7496 (live)
echo   2. Global Configuration -^> API -^> Precautions:
echo      勾选 "Bypass Order Precautions for API Orders"
echo      ^(否则同一合约第二笔订单会被当重复单拒绝^)
echo.
echo   然后双击 start.bat 启动。
echo ============================================================
pause
exit /b 0

:err
echo.
echo [X] 配置失败, 见上方报错。
pause
exit /b 1

:badpy
echo.
echo [X] python 版本太低: %PYVER%  (需要 3.13+)
echo     当前解释器: %PYEXE%
echo.
echo     若你已经装了 3.13, 那说明 PATH 上排在前面的是另一个 python。
echo     注意 Windows 拼 PATH 的顺序是「系统级在前, 用户级在后」, 而装 Python 时
echo     勾的 "Add to PATH" 只加到用户级 —— 会被系统级的旧 python 压住, 怎么装都不生效。
echo     修法: 把 Python 3.13 目录插到「系统级」PATH 最前面 (需管理员), 或改用 py -3.13。
pause
exit /b 1

:nopip
echo.
echo [X] 这个 python 没有 pip: %PYEXE% (%PYVER%)
echo     多半是 PATH 上排在前面的另一个发行版 (如 msys2 的 mingw32 python)。
echo     修法同上: 把 Python 3.13 插到系统级 PATH 最前面, 或用 py -3.13 -m pip 手动装。
pause
exit /b 1
