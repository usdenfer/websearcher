[CmdletBinding()]
param(
    [Alias("Host")]
    [string]$HostName = "127.0.0.1",

    [ValidateRange(1, 65535)]
    [int]$Port = 7100,

    [switch]$NoBrowser,

    [ValidateRange(1, 300)]
    [int]$ReadyTimeoutSeconds = 30
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Url = "http://${HostName}:${Port}/"

function Get-ListeningProcessIds {
    param([int]$LocalPort)

    $connections = @(Get-NetTCPConnection -LocalPort $LocalPort -State Listen -ErrorAction SilentlyContinue)
    return @($connections |
        Where-Object { $_.OwningProcess -gt 0 } |
        Select-Object -ExpandProperty OwningProcess -Unique)
}

function Stop-PortListeners {
    param([int]$LocalPort)

    foreach ($listenerPid in @(Get-ListeningProcessIds -LocalPort $LocalPort)) {
        if ($listenerPid -eq $PID) {
            throw "Refusing to stop the launcher process ($PID) on port $LocalPort."
        }

        Write-Host "Stopping process tree $listenerPid listening on port $LocalPort..."
        & "$env:SystemRoot\System32\taskkill.exe" /PID $listenerPid /T /F | Out-Host
        if ($LASTEXITCODE -ne 0) {
            throw "taskkill failed for process $listenerPid with exit code $LASTEXITCODE."
        }
    }

    $deadline = (Get-Date).AddSeconds(5)
    do {
        $remainingPids = @(Get-ListeningProcessIds -LocalPort $LocalPort)
        if ($remainingPids.Count -eq 0) {
            return
        }
        Start-Sleep -Milliseconds 100
    } while ((Get-Date) -lt $deadline)

    throw "Timed out waiting for port $LocalPort to become free. Remaining PIDs: $($remainingPids -join ', ')."
}

function Resolve-ProjectPython {
    $venvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython) {
        return $venvPython
    }

    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $pythonCommand) {
        throw "Python was not found. Create .venv or install Python."
    }
    return $pythonCommand.Source
}

Set-Location -LiteralPath $ProjectRoot
Stop-PortListeners -LocalPort $Port
$Python = Resolve-ProjectPython

Write-Host "Starting debug server at $Url"
Write-Host "Mode: single process (--no-reload)"

$serverProcess = $null
try {
    $serverProcess = Start-Process -FilePath $Python `
        -ArgumentList @("server.py", "--no-reload", "--host", $HostName, "--port", $Port.ToString()) `
        -WorkingDirectory $ProjectRoot -NoNewWindow -PassThru

    $isReady = $false
    $readyDeadline = (Get-Date).AddSeconds($ReadyTimeoutSeconds)
    while ((Get-Date) -lt $readyDeadline) {
        $serverProcess.Refresh()
        if ($serverProcess.HasExited) {
            throw "Server exited before becoming ready with exit code $($serverProcess.ExitCode)."
        }

        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 2
            if ($response.StatusCode -eq 200) {
                $isReady = $true
                break
            }
        } catch {
            Start-Sleep -Milliseconds 250
            continue
        }

        Start-Sleep -Milliseconds 250
    }

    $serverProcess.Refresh()
    if ($serverProcess.HasExited) {
        throw "Server exited before becoming ready with exit code $($serverProcess.ExitCode)."
    }

    if ($isReady) {
        Write-Host "Server is ready at $Url"
        if (-not $NoBrowser) {
            try {
                Start-Process $Url
            } catch {
                Write-Warning "Could not open the browser: $($_.Exception.Message)"
            }
        }
    } else {
        Write-Warning "Timed out waiting for $Url; the service is still running."
    }

    $serverProcess.WaitForExit()
    exit $serverProcess.ExitCode
} finally {
    if ($serverProcess -and -not $serverProcess.HasExited) {
        try {
            & "$env:SystemRoot\System32\taskkill.exe" /PID $serverProcess.Id /T /F *> $null
        } catch {
        }
    }
}
