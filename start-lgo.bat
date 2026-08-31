@echo off
setlocal

set "LGO_ROOT=%~dp0"
set "PORT=7865"
set "LGO_PYTHON=%LGO_ROOT%.venv\Scripts\python.exe"
set "LGO_RUNS_DIR=%LGO_ROOT%runs"
set "LGO_LOG_DIR=%LGO_ROOT%logs"
set "LGO_CACHE_DIR=%LGO_ROOT%.cache"
set "HF_HOME=%LGO_CACHE_DIR%\huggingface"
set "HF_HUB_CACHE=%HF_HOME%\hub"
set "TRANSFORMERS_CACHE=%HF_HOME%\transformers"
set "XDG_CACHE_HOME=%LGO_CACHE_DIR%"
set "PYTHONUTF8=1"

if not exist "%LGO_RUNS_DIR%" mkdir "%LGO_RUNS_DIR%"
if not exist "%LGO_LOG_DIR%" mkdir "%LGO_LOG_DIR%"
if not exist "%HF_HOME%" mkdir "%HF_HOME%"
if not exist "%HF_HUB_CACHE%" mkdir "%HF_HUB_CACHE%"
if not exist "%TRANSFORMERS_CACHE%" mkdir "%TRANSFORMERS_CACHE%"

if not exist "%LGO_PYTHON%" (
  echo LGO virtual environment was not found: %LGO_PYTHON%
  echo Run scripts\setup_venv.ps1 first.
  pause
  exit /b 1
)

cd /d "%LGO_ROOT%"
start "" "http://localhost:%PORT%/"
"%LGO_PYTHON%" lgo_server.py --host 127.0.0.1 --port %PORT%

echo.
echo LGO stopped.
pause
