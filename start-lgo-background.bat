@echo off
setlocal

set "LGO_ROOT=%~dp0"
set "LGO_PREFERRED_ROOT=E:\AI\Projects\LGO"
if exist "%LGO_PREFERRED_ROOT%\lgo_server.py" set "LGO_ROOT=%LGO_PREFERRED_ROOT%\"
if defined LGO_HOME if exist "%LGO_HOME%\lgo_server.py" set "LGO_ROOT=%LGO_HOME%\"
set "PORT=7865"
set "LGO_PYTHON=%LGO_ROOT%.venv\Scripts\python.exe"
set "LGO_RUNS_DIR=%LGO_ROOT%runs"
set "LGO_LOG_DIR=%LGO_ROOT%logs"
set "LGO_CACHE_DIR=%LGO_ROOT%.cache"
set "LGO_START_SCRIPT=%LGO_ROOT%scripts\start_background.ps1"
set "LGO_SETUP_SCRIPT=%LGO_ROOT%scripts\setup_venv.ps1"
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
  if not exist "%LGO_SETUP_SCRIPT%" (
    echo Setup script was not found:
    echo   %LGO_SETUP_SCRIPT%
    pause
    exit /b 1
  )
  echo.
  choice /C YN /M "Create .venv now with scripts\setup_venv.ps1?"
  if errorlevel 2 exit /b 1
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%LGO_SETUP_SCRIPT%"
  if errorlevel 1 (
    echo Failed to create LGO virtual environment.
    pause
    exit /b 1
  )
  if not exist "%LGO_PYTHON%" (
    echo Setup finished, but Python was still not found:
    echo   %LGO_PYTHON%
    pause
    exit /b 1
  )
)

if not exist "%LGO_START_SCRIPT%" (
  echo LGO background start helper was not found:
  echo   %LGO_START_SCRIPT%
  pause
  exit /b 1
)

start "" /min powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%LGO_START_SCRIPT%"
exit /b 0
