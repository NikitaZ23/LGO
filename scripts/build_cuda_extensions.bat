@echo off
setlocal

set "LGO_ROOT=%~dp0.."
set "PYTHON=%LGO_ROOT%\.venv\Scripts\python.exe"
set "VSDEVCMD=E:\VS\Common7\Tools\VsDevCmd.bat"
set "RASTERIZER_DIR=%LGO_ROOT%\vendor\Hunyuan3D-2.1\hy3dpaint\custom_rasterizer"
set "EXPECTED_CUDA_HOME=C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.4"

if not exist "%PYTHON%" (
  echo LGO venv Python was not found:
  echo   %PYTHON%
  pause
  exit /b 1
)

if not exist "%VSDEVCMD%" (
  echo Visual Studio developer command file was not found:
  echo   %VSDEVCMD%
  pause
  exit /b 1
)

if exist "%EXPECTED_CUDA_HOME%\bin\nvcc.exe" (
  set "CUDA_HOME=%EXPECTED_CUDA_HOME%"
) else (
  if not defined CUDA_HOME (
    if defined CUDA_PATH set "CUDA_HOME=%CUDA_PATH%"
  )
)

if not defined CUDA_HOME (
  echo CUDA Toolkit was not found.
  echo Install CUDA Toolkit 12.4 for the current torch cu124 runtime, then run this file again.
  echo Expected nvcc.exe at:
  echo   C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.4\bin\nvcc.exe
  pause
  exit /b 1
)

if not exist "%CUDA_HOME%\bin\nvcc.exe" (
  echo nvcc.exe was not found at:
  echo   %CUDA_HOME%\bin\nvcc.exe
  pause
  exit /b 1
)

"%PYTHON%" -c "import os, re, subprocess, sys, torch; cuda_home=os.environ.get('CUDA_HOME', ''); nvcc=os.path.join(cuda_home, 'bin', 'nvcc.exe'); out=subprocess.check_output([nvcc, '--version'], text=True, errors='replace'); match=re.search(r'release ([0-9]+\.[0-9]+)', out); nvcc_cuda=match.group(1) if match else 'unknown'; torch_cuda=torch.version.cuda or 'none'; print(f'PyTorch CUDA: {torch_cuda}'); print(f'nvcc CUDA: {nvcc_cuda}'); sys.exit(0 if torch_cuda == nvcc_cuda else 2)"
if errorlevel 1 (
  echo CUDA version mismatch.
  echo This LGO venv currently uses torch cu124, so install CUDA Toolkit 12.4 or rebuild the venv for your Toolkit version.
  pause
  exit /b 1
)

if not exist "%RASTERIZER_DIR%\setup.py" (
  echo custom_rasterizer setup.py was not found:
  echo   %RASTERIZER_DIR%\setup.py
  pause
  exit /b 1
)

call "%VSDEVCMD%" -arch=x64 -host_arch=x64
if errorlevel 1 (
  echo Failed to initialize Visual Studio build environment.
  pause
  exit /b 1
)

set "PATH=%CUDA_HOME%\bin;%CUDA_HOME%\libnvvp;%PATH%"
set "DISTUTILS_USE_SDK=1"

cd /d "%RASTERIZER_DIR%"
echo Building custom_rasterizer with:
echo   Python: %PYTHON%
echo   CUDA_HOME: %CUDA_HOME%
echo   MSVC: %VCToolsVersion%

"%PYTHON%" -m pip install --no-build-isolation -e .
if errorlevel 1 (
  echo custom_rasterizer build failed.
  pause
  exit /b 1
)

"%PYTHON%" -c "import torch; import custom_rasterizer; import custom_rasterizer_kernel; print('custom_rasterizer ok')"
if errorlevel 1 (
  echo custom_rasterizer import check failed.
  pause
  exit /b 1
)

echo CUDA extensions are ready.
pause
