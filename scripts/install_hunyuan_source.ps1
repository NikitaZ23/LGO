$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$VendorRoot = Join-Path $ProjectRoot "vendor"
$Target = Join-Path $VendorRoot "Hunyuan3D-2.1"

New-Item -ItemType Directory -Force -Path $VendorRoot | Out-Null

if (Test-Path -LiteralPath $Target) {
    Write-Host "Hunyuan3D source already exists: $Target"
    exit 0
}

git clone https://github.com/Tencent-Hunyuan/Hunyuan3D-2.1.git $Target

