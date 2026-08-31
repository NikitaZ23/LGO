$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$CacheRoot = Join-Path $ProjectRoot ".cache"
$HfHome = Join-Path $CacheRoot "huggingface"

$env:HF_HOME = $HfHome
$env:HF_HUB_CACHE = Join-Path $HfHome "hub"
$env:TRANSFORMERS_CACHE = Join-Path $HfHome "transformers"
$env:XDG_CACHE_HOME = $CacheRoot
$env:PYTHONUTF8 = "1"

New-Item -ItemType Directory -Force -Path $env:HF_HOME, $env:HF_HUB_CACHE, $env:TRANSFORMERS_CACHE | Out-Null

if (-not (Test-Path -LiteralPath $Python)) {
    throw "LGO virtual environment was not found at $Python. Run scripts\setup_venv.ps1 first."
}

& $Python "$ProjectRoot\scripts\check_environment.py"
