$ErrorActionPreference = "Stop"

function Write-LgoStartLog {
    param([string] $Message)

    $logDir = $env:LGO_LOG_DIR
    if (-not $logDir) {
        $logDir = Join-Path ([IO.Path]::GetFullPath($PSScriptRoot)) "..\logs"
    }
    if (-not (Test-Path -LiteralPath $logDir)) {
        New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    }

    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath (Join-Path $logDir "service-start.log") -Encoding UTF8 -Value "[$stamp] $Message"
}

function Test-LgoPort {
    param([int] $Port)

    $client = [Net.Sockets.TcpClient]::new()
    try {
        $connect = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        if (-not $connect.AsyncWaitHandle.WaitOne(250, $false)) {
            return $false
        }
        $client.EndConnect($connect)
        return $client.Connected
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

try {
    $port = if ($env:PORT) { [int] $env:PORT } else { 7865 }
    $root = if ($env:LGO_ROOT) {
        [IO.Path]::GetFullPath($env:LGO_ROOT)
    } else {
        [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
    }
    $python = if ($env:LGO_PYTHON) {
        $env:LGO_PYTHON
    } else {
        Join-Path $root ".venv\Scripts\python.exe"
    }
    $server = Join-Path $root "lgo_server.py"
    $logDir = if ($env:LGO_LOG_DIR) { $env:LGO_LOG_DIR } else { Join-Path $root "logs" }

    if (-not (Test-Path -LiteralPath $logDir)) {
        New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    }

    if (-not (Test-Path -LiteralPath $python)) {
        throw "LGO virtual environment was not found: $python"
    }
    if (-not (Test-Path -LiteralPath $server)) {
        throw "LGO server script was not found: $server"
    }

    if (Test-LgoPort -Port $port) {
        Write-LgoStartLog "Port $port is already open. Browser opened without starting another server."
        Start-Process "http://localhost:$port/"
        exit 0
    }

    $stdoutLog = Join-Path $logDir "service.out.log"
    $stderrLog = Join-Path $logDir "service.err.log"
    $serverArg = '"' + $server.Replace('"', '\"') + '"'
    $arguments = @($serverArg, "--host", "127.0.0.1", "--port", [string] $port)

    $process = Start-Process `
        -FilePath $python `
        -ArgumentList $arguments `
        -WorkingDirectory $root `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutLog `
        -RedirectStandardError $stderrLog `
        -PassThru

    Write-LgoStartLog "Started server process PID $($process.Id) on port $port."

    for ($attempt = 0; $attempt -lt 80; $attempt++) {
        Start-Sleep -Milliseconds 250
        if (Test-LgoPort -Port $port) {
            Write-LgoStartLog "Server became ready on port $port."
            Start-Process "http://localhost:$port/"
            exit 0
        }
        if ($process.HasExited) {
            Write-LgoStartLog "Server process exited early with code $($process.ExitCode). See service.err.log."
            exit 1
        }
    }

    Write-LgoStartLog "Server process PID $($process.Id) did not become ready within 20 seconds."
    exit 1
} catch {
    Write-LgoStartLog "ERROR: $($_.Exception.Message)"
    exit 1
}
