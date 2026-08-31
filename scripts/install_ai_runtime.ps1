param(
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Venv = Join-Path $ProjectRoot ".venv"
$VenvPython = Join-Path $Venv "Scripts\python.exe"
$Hunyuan = Join-Path $ProjectRoot "vendor\Hunyuan3D-2.1"
$Wheelhouse = Join-Path $ProjectRoot "wheelhouse"
$FilteredRequirements = Join-Path $ProjectRoot "requirements.hunyuan.windows.txt"
$CacheRoot = Join-Path $ProjectRoot ".cache"
$HfHome = Join-Path $CacheRoot "huggingface"

$env:HF_HOME = $HfHome
$env:HF_HUB_CACHE = Join-Path $HfHome "hub"
$env:TRANSFORMERS_CACHE = Join-Path $HfHome "transformers"
$env:XDG_CACHE_HOME = $CacheRoot
$env:PYTHONUTF8 = "1"

New-Item -ItemType Directory -Force -Path $env:HF_HOME, $env:HF_HUB_CACHE, $env:TRANSFORMERS_CACHE | Out-Null

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,

        [Parameter(ValueFromRemainingArguments = $true)]
        [string[]]$Arguments
    )

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code $LASTEXITCODE`: $FilePath $($Arguments -join ' ')"
    }
}

if (-not $Python) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $Python = (& py -3.10 -c "import sys; print(sys.executable)" 2>$null)
    }
    if (-not $Python -and (Get-Command python -ErrorAction SilentlyContinue)) {
        $Python = (Get-Command python).Source
    }
}
$Python = "$Python".Trim()

if (-not (Test-Path -LiteralPath $Hunyuan)) {
    throw "Hunyuan3D source was not found. Run scripts\install_hunyuan_source.ps1 first."
}

if (-not (Test-Path -LiteralPath $VenvPython)) {
    if (-not (Test-Path -LiteralPath $Python)) {
        throw "Python 3.10 was not found. Install Python 3.10 or pass -Python C:\Path\To\python.exe"
    }
    $Version = (& $Python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null)
    if ("$Version".Trim() -ne "3.10") {
        throw "Python 3.10 is required, but $Python reported $Version"
    }
    Invoke-Checked $Python -m venv $Venv
}

Invoke-Checked $VenvPython -m pip install -U pip

$TorchWheel = Join-Path $Wheelhouse "torch-2.5.1+cu124-cp310-cp310-win_amd64.whl"
$TorchvisionWheel = Join-Path $Wheelhouse "torchvision-0.20.1+cu124-cp310-cp310-win_amd64.whl"
$TorchaudioWheel = Join-Path $Wheelhouse "torchaudio-2.5.1+cu124-cp310-cp310-win_amd64.whl"

if ((Test-Path -LiteralPath $TorchWheel) -and (Test-Path -LiteralPath $TorchvisionWheel) -and (Test-Path -LiteralPath $TorchaudioWheel)) {
    Invoke-Checked $VenvPython -m pip install $TorchWheel $TorchvisionWheel $TorchaudioWheel
} else {
    Invoke-Checked $VenvPython -m pip install torch==2.5.1 torchvision==0.20.1 torchaudio==2.5.1 --index-url https://download.pytorch.org/whl/cu124
}

Get-Content -LiteralPath (Join-Path $Hunyuan "requirements.txt") |
    Where-Object { $_ -notmatch '^\s*bpy\s*==' -and $_ -notmatch '^\s*deepspeed\b' } |
    Set-Content -LiteralPath $FilteredRequirements -Encoding utf8

Invoke-Checked $VenvPython -m pip install -r $FilteredRequirements

$Rasterizer = Join-Path $Hunyuan "hy3dpaint\custom_rasterizer"
if (Test-Path -LiteralPath $Rasterizer) {
    if ($env:CUDA_HOME -or (Get-Command nvcc -ErrorAction SilentlyContinue)) {
        Invoke-Checked $VenvPython "-m" "pip" "install" "--no-build-isolation" "-e" $Rasterizer
    } else {
        Write-Host "Skipping custom_rasterizer: CUDA Toolkit was not found. Install CUDA Toolkit and set CUDA_HOME to build it."
    }
}

$Renderer = Join-Path $Hunyuan "hy3dpaint\DifferentiableRenderer"
if (Test-Path -LiteralPath $Renderer) {
    Write-Host "DifferentiableRenderer exists at $Renderer"
    Write-Host "If texture generation fails later, compile it from a shell with bash support:"
    Write-Host "  cd $Renderer"
    Write-Host "  bash compile_mesh_painter.sh"
}
