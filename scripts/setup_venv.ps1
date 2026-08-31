param(
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Venv = Join-Path $ProjectRoot ".venv"

if (-not $Python) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $Python = (& py -3.10 -c "import sys; print(sys.executable)" 2>$null)
    }
    if (-not $Python -and (Get-Command python -ErrorAction SilentlyContinue)) {
        $Python = (Get-Command python).Source
    }
}
$Python = "$Python".Trim()

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python 3.10 was not found. Install Python 3.10 or pass -Python C:\Path\To\python.exe"
}

$Version = (& $Python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null)
if ("$Version".Trim() -ne "3.10") {
    throw "Python 3.10 is required, but $Python reported $Version"
}

& $Python -m venv $Venv
& "$Venv\Scripts\python.exe" -m pip install -U pip
& "$Venv\Scripts\python.exe" -m pip install -r "$ProjectRoot\requirements.txt"
