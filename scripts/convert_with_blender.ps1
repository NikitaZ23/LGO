param(
    [Parameter(Mandatory = $true)]
    [string]$InputPath,

    [Parameter(Mandatory = $true)]
    [string]$OutputPath,

    [string]$Blender = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot

if (-not $Blender) {
    $ConfigPath = Join-Path $ProjectRoot "config\lgo_config.json"
    $Config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
    $Blender = $Config.paths.blender
}

if (-not (Test-Path -LiteralPath $Blender)) {
    throw "Blender was not found at $Blender"
}

& $Blender --background --python "$ProjectRoot\tools\blender_convert.py" -- $InputPath $OutputPath
