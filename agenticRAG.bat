@echo off
REM ============================================================
REM Agentic RAG - 一键快速启动（Windows）
REM
REM 用法：在项目根目录双击或在 cmd 中执行 agenticRAG.bat
REM 前置：已完成 5.2（.env 已创建并填写密钥）
REM 作用：docker compose up -d + 弹出 Backend / Frontend 两个窗口
REM
REM 注意：本脚本只做"启动"，不做"首次安装"。
REM       首次使用请先手动运行：
REM         uv sync
REM         cd src\web && pnpm install
REM ============================================================

setlocal
chcp 65001 >nul
title Agentic RAG - Quick Start

REM 切换到脚本所在目录（无论从哪里调用都生效）
cd /d "%~dp0"

echo.
echo === Agentic RAG 一键启动 ===
echo.

REM ---------- 1. 前置检查：.env ----------
if not exist ".env" (
    echo [X] .env 不存在
    echo.
    echo     请先按 README 5.2 执行：
    echo         cp .env.example .env
    echo     然后编辑 .env，至少填写：
    echo         POSTGRES_PASSWORD / REDIS_PASSWORD / JWT_SECRET /
    echo         LITELLM_MASTER_KEY / DASHSCOPE_API_KEY /
    echo         GRAFANA_ADMIN_PASSWORD / QDRANT_API_KEY
    echo.
    pause
    exit /b 1
)

REM ---------- 2. 首次运行提示（非阻塞） ----------
if not exist ".venv" (
    echo [!] .venv 不存在 —— 后端首次启动可能失败
    echo     请先在另一终端运行： uv sync
    echo.
)

if not exist "src\web\node_modules" (
    echo [!] src\web\node_modules 不存在 —— 前端首次启动可能失败
    echo     请先在另一终端运行： cd src\web ^&^& pnpm install
    echo.
)

REM ---------- 3. 启动基础设施 ----------
echo [1/3] 启动 Docker 基础设施（PostgreSQL / Redis / Qdrant / LiteLLM / DeepDoc / ...）
docker compose up -d
if errorlevel 1 (
    echo [X] docker compose 启动失败，请确认 Docker Desktop 正在运行
    pause
    exit /b 1
)

echo.
echo [2/3] 等待服务就绪 ...
ping -n 8 127.0.0.1 >nul
docker compose ps

REM ---------- 4. 弹出 Backend / Frontend 窗口 ----------
echo.
echo [3/3] 弹出后端与前端窗口 ...
start "AgenticRAG-Backend"  cmd /k "title AgenticRAG-Backend  &&  uv run python -m agentic_rag_project"
start "AgenticRAG-Frontend" cmd /k "title AgenticRAG-Frontend &&  cd /d src\web && pnpm dev"

REM ---------- 5. 收尾 ----------
echo.
echo === 启动完成 ===
echo   API      :  http://localhost:8000    (Swagger: /docs)
echo   Frontend :  http://localhost:5173
echo   Grafana  :  http://localhost:3000
echo   Qdrant   :  http://localhost:6333/dashboard
echo.
echo 日志见刚才弹出的两个窗口（关闭它们 = 停止对应服务）。
echo 停止所有基础设施： docker compose down
echo.
pause
endlocal
