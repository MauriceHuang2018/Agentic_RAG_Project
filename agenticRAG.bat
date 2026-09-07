@echo off
REM ============================================================
REM Agentic RAG - 菜单式启动脚本（Windows）
REM
REM 用法：在项目根目录执行 agenticRAG.bat
REM
REM 菜单：
REM   [1] 首次安装 - 创建 .env + uv sync + pnpm install
REM   [2] 一键启动 - docker compose up + Backend / Frontend 窗口
REM   [3] 停止所有 - docker compose down + 关闭弹出的窗口
REM   [0] 退出
REM ============================================================

setlocal
chcp 65001 >nul

cd /d "%~dp0"

:MENU
echo.
echo === Agentic RAG ===
echo 当前目录: %CD%
echo.
echo   [1] 首次安装 (创建 .env + uv sync + pnpm install)
echo   [2] 一键启动 (docker compose up + Backend / Frontend 窗口)
echo   [3] 停止所有 (docker compose down + 关闭窗口)
echo   [0] 退出
echo.
set /p choice="请选择 (0-3): "

if "%choice%"=="1" goto INSTALL
if "%choice%"=="2" goto START
if "%choice%"=="3" goto STOP
if "%choice%"=="0" goto END
echo 无效选择
pause
goto MENU

REM ============================================================
REM [1] 首次安装
REM ============================================================
:INSTALL
if not exist ".env.example" (
    echo [X] .env.example 不存在，无法创建 .env
    goto FAIL
)
if not exist ".env" (
    copy /Y .env.example .env
    echo [!] 已创建 .env —— 请编辑填入密钥后再启动
) else (
    echo [i] .env 已存在，跳过复制
)
echo.
if exist ".venv" (
    echo [i] .venv 已存在，跳过 uv sync
) else (
    echo [1/2] uv sync ...
    uv sync
    if errorlevel 1 goto FAIL
)
if exist "src\web\node_modules" (
    echo [i] src\web\node_modules 已存在，跳过 pnpm install
) else (
    echo [2/2] pnpm install (src\web) ...
    cd /d "%~dp0src\web"
    pnpm install
    if errorlevel 1 (
        cd /d "%~dp0"
        goto FAIL
    )
    cd /d "%~dp0"
)
echo.
echo === 安装完成 ===
echo   如 .env 是新建的，请先填密钥再选 [2] 启动
pause
goto MENU

REM ============================================================
REM [2] 一键启动
REM ============================================================
:START
if not exist ".env" (
    echo [X] .env 不存在 —— 请先选 [1] 完成首次安装
    pause
    goto MENU
)
if not exist ".venv" (
    echo [!] .venv 不存在 —— 后端窗口启动会失败，请先选 [1] 安装
)
if not exist "src\web\node_modules" (
    echo [!] src\web\node_modules 不存在 —— 前端窗口启动会失败，请先选 [1] 安装
)
echo.
echo [1/3] 启动 Docker 基础设施 ...
docker compose up -d
if errorlevel 1 (
    echo [X] docker compose 启动失败，请确认 Docker Desktop 正在运行
    pause
    goto MENU
)
echo.
echo [2/3] 等待服务就绪 ...
ping -n 8 127.0.0.1 >nul
docker compose ps
echo.
echo [3/3] 弹出 Backend / Frontend 窗口 ...
start "AgenticRAG-Backend"  cmd /k "title AgenticRAG-Backend  &&  uv run python -m agentic_rag_project"
start "AgenticRAG-Frontend" cmd /k "title AgenticRAG-Frontend &&  cd /d src\web && pnpm dev"
echo.
echo === 启动完成 ===
echo   API      :  http://localhost:8000    (Swagger: /docs)
echo   Frontend :  http://localhost:5173
echo   Grafana  :  http://localhost:3000
echo   Qdrant   :  http://localhost:6333/dashboard
echo.
echo 日志见弹出的两个窗口。可选 [3] 停止。
goto END

REM ============================================================
REM [3] 停止所有
REM ============================================================
:STOP
echo 停止 Docker 基础设施 ...
docker compose down
echo.
echo 关闭 Backend / Frontend 窗口 ...
taskkill /FI "WINDOWTITLE eq AgenticRAG-Backend*"  /T /F 2>nul
taskkill /FI "WINDOWTITLE eq AgenticRAG-Frontend*" /T /F 2>nul
echo === 已停止 ===
pause
goto MENU

REM ============================================================
REM 失败 / 退出
REM ============================================================
:FAIL
echo.
echo [X] 失败 —— 请检查上方错误输出
pause
goto MENU

:END
endlocal
