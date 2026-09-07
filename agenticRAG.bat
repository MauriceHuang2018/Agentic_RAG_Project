@echo off
REM ============================================================
REM Agentic RAG - Menu-driven launcher (Windows)
REM
REM Run from project root:  agenticRAG.bat
REM
REM Menu:
REM   [1] First-time install  - copy .env + uv sync + pnpm install
REM   [2] Start               - docker compose up + Backend/Frontend
REM   [3] Stop                - docker compose down + close windows
REM   [0] Exit
REM
REM NOTE: this file is ASCII-only on purpose. cmd.exe parses .bat
REM       files in the console codepage (ANSI), not UTF-8, so any
REM       non-ASCII bytes get misinterpreted as garbled tokens and
REM       break the script. Keep it English.
REM ============================================================

setlocal
chcp 65001 >nul

cd /d "%~dp0"

:MENU
echo.
echo === Agentic RAG ===
echo CWD: %CD%
echo.
echo   [1] First-time install  (.env + uv sync + pnpm install)
echo   [2] Start               (docker compose up + Backend/Frontend windows)
echo   [3] Stop                (docker compose down + close windows)
echo   [0] Exit
echo.
set /p choice="Choose (0-3): "

if "%choice%"=="1" goto INSTALL
if "%choice%"=="2" goto START
if "%choice%"=="3" goto STOP
if "%choice%"=="0" goto END
echo Invalid choice.
pause
goto MENU

REM ============================================================
REM [1] First-time install
REM ============================================================
:INSTALL
if not exist ".env.example" (
    echo [X] .env.example not found, cannot create .env
    goto FAIL
)
if not exist ".env" (
    copy /Y .env.example .env
    echo [!] Created .env - edit secrets before choosing [2] Start
) else (
    echo [i] .env already exists, skipping copy
)
echo.
if exist ".venv" (
    echo [i] .venv already exists, skipping uv sync
) else (
    echo [1/2] uv sync ...
    uv sync
    if errorlevel 1 goto FAIL
)
if exist "src\web\node_modules" (
    echo [i] src\web\node_modules already exists, skipping pnpm install
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
echo === Install complete ===
echo   If .env was newly created, edit secrets before choosing [2] Start
pause
goto MENU

REM ============================================================
REM [2] Start
REM ============================================================
:START
if not exist ".env" (
    echo [X] .env missing - please choose [1] first
    pause
    goto MENU
)
if not exist ".venv" (
    echo [!] .venv missing - backend window will fail, choose [1] first
)
if not exist "src\web\node_modules" (
    echo [!] src\web\node_modules missing - frontend window will fail, choose [1] first
)
echo.
echo [1/3] Starting Docker infra ...
docker compose up -d
if errorlevel 1 (
    echo [X] docker compose failed - is Docker Desktop running?
    pause
    goto MENU
)
echo.
echo [2/3] Waiting for services ...
ping -n 8 127.0.0.1 >nul
docker compose ps
echo.
echo [3/3] Popping Backend / Frontend windows ...
REM /D sets the working dir for the new window; cmd /k runs ONE command
REM (no '&&' chain here - that breaks the outer 'start' line parser).
start "AgenticRAG-Backend"  /D "%~dp0."           cmd /k "uv run python -m agentic_rag_project"
start "AgenticRAG-Frontend" /D "%~dp0src\web."     cmd /k "pnpm dev"
echo.
echo === Started ===
echo   API      :  http://localhost:8000    (Swagger: /docs)
echo   Frontend :  http://localhost:5173
echo   Grafana  :  http://localhost:3000
echo   Qdrant   :  http://localhost:6333/dashboard
echo.
echo Logs in the popped windows. Choose [3] to stop.
goto END

REM ============================================================
REM [3] Stop
REM ============================================================
:STOP
echo Stopping Docker infra ...
docker compose down
echo.
echo Closing Backend / Frontend windows ...
taskkill /FI "WINDOWTITLE eq AgenticRAG-Backend"  /T /F 2>nul
taskkill /FI "WINDOWTITLE eq AgenticRAG-Frontend" /T /F 2>nul
echo === Stopped ===
pause
goto MENU

REM ============================================================
REM Failure / exit
REM ============================================================
:FAIL
echo.
echo [X] Failed - check the error output above
pause
goto MENU

:END
endlocal
