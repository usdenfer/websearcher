[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 7100,

    [Alias("Host")]
    [ValidateScript({
        $candidate = [string]$_
        if ([string]::IsNullOrWhiteSpace($candidate)) {
            throw "HostName cannot be empty or whitespace."
        }
        if ($candidate.StartsWith("-")) {
            throw "HostName cannot start with '-'."
        }
        if ($candidate -match "\s") {
            throw "HostName cannot contain whitespace."
        }
        if ($candidate.Contains(":")) {
            throw "HostName cannot use IPv6 syntax."
        }

        if ($candidate -match "^\d{1,3}(?:\.\d{1,3}){3}$") {
            foreach ($octet in $candidate.Split(".")) {
                if ([int]$octet -gt 255) {
                    throw "HostName is not a valid IPv4 address."
                }
            }
            return $true
        }
        if ($candidate -match "^[0-9.]+$") {
            throw "HostName is not a valid IPv4 address."
        }

        $dnsName = if ($candidate.EndsWith(".")) {
            $candidate.Substring(0, $candidate.Length - 1)
        } else {
            $candidate
        }
        $dnsPattern = "^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(?:\.(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?))*$"
        if ($dnsName -notmatch $dnsPattern) {
            throw "HostName is not a valid DNS hostname."
        }
        return $true
    })]
    [string]$HostName = "127.0.0.1",

    [switch]$NoBrowser,

    [ValidateRange(1, 300)]
    [int]$ReadyTimeoutSeconds = 30,

    [Parameter(DontShow = $true)]
    [ValidateRange(0, 2147483647)]
    [int]$ExpectedListenerProcessId = 0,

    [Parameter(DontShow = $true)]
    [long]$ExpectedListenerStartTimeUtcTicks = 0,

    [Parameter(DontShow = $true)]
    [switch]$TestInvalidServerArgument,

    [Parameter(DontShow = $true)]
    [switch]$TestOnlyImportFunctions
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ConnectHost = if ($HostName -eq "0.0.0.0") {
    "127.0.0.1"
} else {
    $HostName
}
$Url = "http://${ConnectHost}:${Port}/"

function Get-ListeningProcessIds {
    param([int]$LocalPort)

    $connections = @(Get-NetTCPConnection `
        -LocalPort $LocalPort `
        -State Listen `
        -ErrorAction SilentlyContinue)
    return @($connections |
        Where-Object { $_.OwningProcess -gt 0 } |
        Select-Object -ExpandProperty OwningProcess -Unique)
}

function Get-ProcessStartTimeUtcTicks {
    param([int]$ProcessId)

    try {
        $process = Get-Process -Id $ProcessId -ErrorAction Stop
        return [long]$process.StartTime.ToUniversalTime().Ticks
    } catch {
        return $null
    }
}

function Get-CimProcess {
    param([int]$ProcessId)

    return Get-CimInstance `
        -ClassName Win32_Process `
        -Filter "ProcessId = $ProcessId" `
        -ErrorAction Stop
}

function Get-CimProcessCreationTimeUtcTicks {
    param($ProcessInfo)

    try {
        if ($null -eq $ProcessInfo -or
                $null -eq $ProcessInfo.CreationDate) {
            return $null
        }
        return [long](
            ([datetime]$ProcessInfo.CreationDate).ToUniversalTime().Ticks
        )
    } catch {
        return $null
    }
}

function Get-ListeningProcessSnapshots {
    param([int]$LocalPort)

    foreach ($listenerPid in @(Get-ListeningProcessIds -LocalPort $LocalPort)) {
        $startTimeUtcTicks = Get-ProcessStartTimeUtcTicks `
            -ProcessId $listenerPid
        if ($null -eq $startTimeUtcTicks) {
            if (@(Get-ListeningProcessIds -LocalPort $LocalPort) -contains
                    $listenerPid) {
                throw "Could not verify listener process $listenerPid on port $LocalPort."
            }
            continue
        }

        [pscustomobject]@{
            ProcessId = [int]$listenerPid
            StartTimeUtcTicks = [long]$startTimeUtcTicks
        }
    }
}

function Get-LauncherProtectedProcessIds {
    $protectedProcessIds = @{}
    $cursorProcessId = [int]$PID

    while ($cursorProcessId -gt 0) {
        if ($protectedProcessIds.ContainsKey($cursorProcessId)) {
            throw "Could not safely determine the launcher ancestor chain."
        }
        $protectedProcessIds[$cursorProcessId] = $true

        $processInfo = Get-CimProcess -ProcessId $cursorProcessId
        if ($null -eq $processInfo) {
            if ($cursorProcessId -eq $PID) {
                throw "Could not safely determine the launcher ancestor chain."
            }
            break
        }
        $cursorProcessId = [int]$processInfo.ParentProcessId
    }

    return @($protectedProcessIds.Keys | ForEach-Object { [int]$_ })
}

function Test-IsProcessDescendantOrSelf {
    param(
        [int]$CandidateProcessId,
        [int]$RootProcessId
    )

    $visitedProcessIds = @{}
    $cursorProcessId = $CandidateProcessId
    $cursorProcessInfo = $null
    while ($cursorProcessId -gt 0 -and
            -not $visitedProcessIds.ContainsKey($cursorProcessId)) {
        if ($cursorProcessId -eq $RootProcessId) {
            return $true
        }
        $visitedProcessIds[$cursorProcessId] = $true

        if ($null -eq $cursorProcessInfo) {
            $cursorProcessInfo = Get-CimProcess `
                -ProcessId $cursorProcessId
        }
        if ($null -eq $cursorProcessInfo) {
            return $false
        }

        $parentProcessId = [int]$cursorProcessInfo.ParentProcessId
        if ($parentProcessId -le 0) {
            return $false
        }
        $parentProcessInfo = Get-CimProcess `
            -ProcessId $parentProcessId
        if ($null -eq $parentProcessInfo) {
            return $false
        }

        $childCreatedUtcTicks = Get-CimProcessCreationTimeUtcTicks `
            -ProcessInfo $cursorProcessInfo
        $parentCreatedUtcTicks = Get-CimProcessCreationTimeUtcTicks `
            -ProcessInfo $parentProcessInfo
        if ($null -eq $childCreatedUtcTicks -or
                $null -eq $parentCreatedUtcTicks -or
                $parentCreatedUtcTicks -gt $childCreatedUtcTicks) {
            return $false
        }

        $cursorProcessId = $parentProcessId
        $cursorProcessInfo = $parentProcessInfo
    }
    return $false
}

function Test-ProcessIdentity {
    param(
        [int]$ProcessId,
        [long]$ExpectedStartTimeUtcTicks
    )

    $actualStartTimeUtcTicks = Get-ProcessStartTimeUtcTicks `
        -ProcessId $ProcessId
    return (
        $null -ne $actualStartTimeUtcTicks -and
        [long]$actualStartTimeUtcTicks -eq $ExpectedStartTimeUtcTicks
    )
}

function Invoke-ProcessTreeKill {
    param(
        [int]$ProcessId,
        [switch]$Quiet
    )

    $taskkillOutput = @(
        & "$env:SystemRoot\System32\taskkill.exe" `
            /PID $ProcessId /T /F 2>&1
    )
    $taskkillExitCode = $LASTEXITCODE
    if (-not $Quiet) {
        $taskkillOutput | Out-Host
    }
    if ($taskkillExitCode -ne 0) {
        throw "taskkill failed for process $ProcessId with exit code $taskkillExitCode."
    }
}

function Stop-PortListeners {
    param(
        [int]$LocalPort,
        [int]$ExpectedProcessId = 0,
        [long]$ExpectedStartTimeUtcTicks = 0
    )

    if (($ExpectedProcessId -eq 0) -xor
            ($ExpectedStartTimeUtcTicks -eq 0)) {
        throw "Expected listener PID and start time must be supplied together."
    }
    if ($ExpectedStartTimeUtcTicks -lt 0) {
        throw "Expected listener start time cannot be negative."
    }

    $snapshots = @(Get-ListeningProcessSnapshots -LocalPort $LocalPort)
    $protectedProcessIds = @(Get-LauncherProtectedProcessIds)
    foreach ($snapshot in $snapshots) {
        if ($protectedProcessIds -contains $snapshot.ProcessId) {
            throw "Refusing to stop launcher or ancestor process $($snapshot.ProcessId) on port $LocalPort."
        }
    }

    if ($ExpectedProcessId -ne 0) {
        $expectedMatches = (
            $snapshots.Count -eq 1 -and
            $snapshots[0].ProcessId -eq $ExpectedProcessId -and
            $snapshots[0].StartTimeUtcTicks -eq
                $ExpectedStartTimeUtcTicks
        )
        if (-not $expectedMatches) {
            throw "Selected-port listener does not match the expected process identity; refusing to stop it."
        }
    }

    foreach ($snapshot in $snapshots) {
        $currentOwners = @(
            Get-ListeningProcessIds -LocalPort $LocalPort
        )
        if ($currentOwners -notcontains $snapshot.ProcessId) {
            Write-Host "Skipping stale listener process $($snapshot.ProcessId) on port $LocalPort."
            continue
        }

        $currentStartTimeUtcTicks = Get-ProcessStartTimeUtcTicks `
            -ProcessId $snapshot.ProcessId
        if ($null -eq $currentStartTimeUtcTicks) {
            if (@(Get-ListeningProcessIds -LocalPort $LocalPort) -contains
                    $snapshot.ProcessId) {
                throw "Could not verify listener process $($snapshot.ProcessId) on port $LocalPort."
            }
            continue
        }
        if ([long]$currentStartTimeUtcTicks -ne
                $snapshot.StartTimeUtcTicks) {
            throw "Refusing to stop reused process ID $($snapshot.ProcessId) on port $LocalPort."
        }

        $finalOwners = @(
            Get-ListeningProcessIds -LocalPort $LocalPort
        )
        if ($finalOwners -notcontains $snapshot.ProcessId) {
            Write-Host "Skipping stale listener process $($snapshot.ProcessId) on port $LocalPort."
            continue
        }
        $finalStartTimeUtcTicks = Get-ProcessStartTimeUtcTicks `
            -ProcessId $snapshot.ProcessId
        if ($null -eq $finalStartTimeUtcTicks -or
                [long]$finalStartTimeUtcTicks -ne
                    $snapshot.StartTimeUtcTicks) {
            throw "Refusing to stop reused or unverifiable process ID $($snapshot.ProcessId) on port $LocalPort."
        }

        if ($ExpectedProcessId -ne 0) {
            $expectedStillMatches = (
                $finalOwners.Count -eq 1 -and
                $snapshot.ProcessId -eq $ExpectedProcessId -and
                $finalStartTimeUtcTicks -eq
                    $ExpectedStartTimeUtcTicks
            )
            if (-not $expectedStillMatches) {
                throw "Selected-port listener no longer matches the expected process identity; refusing to stop it."
            }
        }

        Write-Host "Stopping process tree $($snapshot.ProcessId) listening on port $LocalPort..."
        Invoke-ProcessTreeKill -ProcessId $snapshot.ProcessId
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

function Test-ServerOwnsPort {
    param(
        [int]$LocalPort,
        [int]$ServerProcessId,
        [long]$ServerStartTimeUtcTicks
    )

    if (-not (Test-ProcessIdentity `
            -ProcessId $ServerProcessId `
            -ExpectedStartTimeUtcTicks $ServerStartTimeUtcTicks)) {
        return $false
    }

    foreach ($listenerPid in @(Get-ListeningProcessIds `
            -LocalPort $LocalPort)) {
        if (Test-IsProcessDescendantOrSelf `
                -CandidateProcessId $listenerPid `
                -RootProcessId $ServerProcessId) {
            return (Test-ProcessIdentity `
                -ProcessId $ServerProcessId `
                -ExpectedStartTimeUtcTicks $ServerStartTimeUtcTicks)
        }
    }
    return $false
}

function Stop-LaunchedServerProcess {
    param(
        [System.Diagnostics.Process]$Process,
        [long]$ExpectedStartTimeUtcTicks
    )

    try {
        $Process.Refresh()
        if ($Process.HasExited) {
            return
        }

        $serverProcessId = $Process.Id
        $actualStartTimeUtcTicks = Get-ProcessStartTimeUtcTicks `
            -ProcessId $serverProcessId
        if ($null -eq $actualStartTimeUtcTicks) {
            $Process.Refresh()
            if (-not $Process.HasExited) {
                Write-Warning "Cleanup could not verify server process $serverProcessId; refusing to stop it."
            }
            return
        }
        if ([long]$actualStartTimeUtcTicks -ne
                $ExpectedStartTimeUtcTicks) {
            Write-Warning "Cleanup found reused server process ID $serverProcessId; refusing to stop it."
            return
        }

        Invoke-ProcessTreeKill -ProcessId $serverProcessId -Quiet
        $null = $Process.WaitForExit(1000)
        $Process.Refresh()
        if (-not $Process.HasExited) {
            Write-Warning "Cleanup could not stop server process $serverProcessId; it may still be running."
        }
    } catch {
        Write-Warning "Cleanup could not stop the launched server process: $($_.Exception.Message)"
    }
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

if (($ExpectedListenerProcessId -eq 0) -xor
        ($ExpectedListenerStartTimeUtcTicks -eq 0)) {
    throw "Expected listener PID and start time must be supplied together."
}
if ($ExpectedListenerStartTimeUtcTicks -lt 0) {
    throw "Expected listener start time cannot be negative."
}
if ($TestOnlyImportFunctions) {
    return
}

Set-Location -LiteralPath $ProjectRoot
Stop-PortListeners `
    -LocalPort $Port `
    -ExpectedProcessId $ExpectedListenerProcessId `
    -ExpectedStartTimeUtcTicks $ExpectedListenerStartTimeUtcTicks
$Python = Resolve-ProjectPython

Write-Host "Starting debug server at $Url"
Write-Host "Mode: single process (--no-reload)"

$serverProcess = $null
$serverStartTimeUtcTicks = 0
try {
    $serverArguments = @(
        "server.py",
        "--no-reload",
        "--host",
        $HostName,
        "--port",
        $Port.ToString()
    )
    if ($TestInvalidServerArgument) {
        $serverArguments += "--start-debug-test-invalid"
    }

    $serverProcess = Start-Process `
        -FilePath $Python `
        -ArgumentList $serverArguments `
        -WorkingDirectory $ProjectRoot `
        -NoNewWindow `
        -PassThru
    # Retaining the native handle is required for reliable ExitCode access in PS 5.1.
    $null = $serverProcess.Handle
    $serverStartTimeUtcTicks = [long](
        $serverProcess.StartTime.ToUniversalTime().Ticks
    )

    $isReady = $false
    $readyDeadline = (Get-Date).AddSeconds($ReadyTimeoutSeconds)
    while ((Get-Date) -lt $readyDeadline) {
        $serverProcess.Refresh()
        if ($serverProcess.HasExited) {
            $serverProcess.WaitForExit()
            $serverExitCode = $serverProcess.ExitCode
            [Console]::Error.WriteLine(
                "Server exited before becoming ready with exit code $serverExitCode."
            )
            exit $serverExitCode
        }

        try {
            $response = Invoke-WebRequest `
                -UseBasicParsing `
                -Uri $Url `
                -TimeoutSec 2
            if ($response.StatusCode -eq 200 -and
                    (Test-ServerOwnsPort `
                        -LocalPort $Port `
                        -ServerProcessId $serverProcess.Id `
                        -ServerStartTimeUtcTicks $serverStartTimeUtcTicks)) {
                $isReady = $true
                break
            }
        } catch {
        }
        Start-Sleep -Milliseconds 250
    }

    $serverProcess.Refresh()
    if ($serverProcess.HasExited) {
        $serverProcess.WaitForExit()
        $serverExitCode = $serverProcess.ExitCode
        [Console]::Error.WriteLine(
            "Server exited before becoming ready with exit code $serverExitCode."
        )
        exit $serverExitCode
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
    if ($null -ne $serverProcess) {
        Stop-LaunchedServerProcess `
            -Process $serverProcess `
            -ExpectedStartTimeUtcTicks $serverStartTimeUtcTicks
    }
}
