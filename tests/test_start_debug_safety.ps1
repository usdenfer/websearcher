[CmdletBinding()]
param(
    [ValidateRange(1024, 65535)]
    [int]$EarlyExitPort = 48241
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$LauncherPath = Join-Path $ProjectRoot "scripts\start-debug.ps1"

function Assert-True {
    param(
        [bool]$Condition,
        [string]$Message
    )

    if (-not $Condition) {
        throw "Assertion failed: $Message"
    }
}

function Assert-Throws {
    param(
        [scriptblock]$Action,
        [string]$MessagePattern,
        [string]$Message
    )

    try {
        & $Action
    } catch {
        Assert-True (
            $_.Exception.Message -match $MessagePattern
        ) "$Message (actual: $($_.Exception.Message))"
        return
    }
    throw "Assertion failed: $Message (no exception was thrown)"
}

$tokens = $null
$parseErrors = $null
$launcherAst = [System.Management.Automation.Language.Parser]::ParseFile(
    $LauncherPath,
    [ref]$tokens,
    [ref]$parseErrors
)
Assert-True ($parseErrors.Count -eq 0) "launcher parses successfully"

$parameterNames = @(
    $launcherAst.ParamBlock.Parameters |
        ForEach-Object { $_.Name.VariablePath.UserPath }
)
Assert-True (
    $parameterNames -contains "TestOnlyImportFunctions"
) "launcher exposes the hidden safe helper-import seam"

$launcherSource = Get-Content -Raw -LiteralPath $LauncherPath
foreach ($hiddenParameterName in @(
        "ExpectedListenerProcessId",
        "ExpectedListenerStartTimeUtcTicks",
        "TestInvalidServerArgument",
        "TestOnlyImportFunctions"
    )) {
    Assert-True (
        $parameterNames -contains $hiddenParameterName
    ) "launcher exposes hidden parameter $hiddenParameterName"
}
Assert-True (
    ([regex]::Matches(
        $launcherSource,
        "\[Parameter\(DontShow\s*=\s*\`$true\)\]"
    )).Count -ge 4
) "test and integration seams are hidden"

foreach ($validHostName in @(
        "127.0.0.1",
        "0.0.0.0",
        "localhost",
        "dev-box.example.com",
        "example.com."
    )) {
    $hostWasRejected = $false
    try {
        & $LauncherPath `
            -HostName $validHostName `
            -TestOnlyImportFunctions |
            Out-Null
    } catch {
        $hostWasRejected = $true
    }
    Assert-True (
        -not $hostWasRejected
    ) "valid host '$validHostName' is accepted without touching a port"
}

foreach ($invalidHostName in @(
        "",
        " ",
        "-127.0.0.1",
        "--help",
        "bad host",
        "999.1.1.1",
        "127.1",
        "1.2.3",
        "::1",
        "[::1]",
        "bad_name.example",
        "example.com..",
        "localhost..."
    )) {
    Assert-Throws `
        -MessagePattern "HostName" `
        -Message "invalid host '$invalidHostName' is rejected" `
        -Action {
            & $LauncherPath `
                -HostName $invalidHostName `
                -TestOnlyImportFunctions |
                Out-Null
        }
}

& {
    . $LauncherPath -TestOnlyImportFunctions

    foreach ($functionName in @(
            "Get-ListeningProcessSnapshots",
            "Get-LauncherProtectedProcessIds",
            "Test-ProcessIdentity",
            "Test-ServerOwnsPort",
            "Stop-LaunchedServerProcess"
        )) {
        Assert-True (
            $null -ne (Get-Command $functionName -ErrorAction SilentlyContinue)
        ) "launcher imports helper $functionName"
    }
}

& {
    . $LauncherPath -TestOnlyImportFunctions
    function Get-CimProcess {
        param([int]$ProcessId)
        if ($ProcessId -eq $PID) {
            return [pscustomobject]@{ ParentProcessId = 1234 }
        }
        return $null
    }

    $protectedProcessIds = @(Get-LauncherProtectedProcessIds)
    Assert-True (
        $protectedProcessIds -contains $PID
    ) "launcher PID remains protected"
    Assert-True (
        $protectedProcessIds -contains 1234
    ) "last known ancestor PID remains protected when its historical parent is gone"
}

& {
    . $LauncherPath -TestOnlyImportFunctions
    function Get-CimProcess {
        param([int]$ProcessId)
        switch ($ProcessId) {
            30 {
                return [pscustomobject]@{
                    ProcessId = 30
                    ParentProcessId = 20
                    CreationDate = [datetime]"2026-01-01T00:00:03Z"
                }
            }
            20 {
                return [pscustomobject]@{
                    ProcessId = 20
                    ParentProcessId = 10
                    CreationDate = [datetime]"2026-01-01T00:00:04Z"
                }
            }
            10 {
                return [pscustomobject]@{
                    ProcessId = 10
                    ParentProcessId = 0
                    CreationDate = [datetime]"2026-01-01T00:00:01Z"
                }
            }
        }
        return $null
    }

    Assert-True (
        -not (Test-IsProcessDescendantOrSelf `
            -CandidateProcessId 30 `
            -RootProcessId 10)
    ) "ancestry rejects a parent PID reused after the child was created"
}

& {
    . $LauncherPath -TestOnlyImportFunctions
    function Get-CimProcess {
        param([int]$ProcessId)
        switch ($ProcessId) {
            30 {
                return [pscustomobject]@{
                    ProcessId = 30
                    ParentProcessId = 20
                    CreationDate = [datetime]"2026-01-01T00:00:03Z"
                }
            }
            20 {
                return [pscustomobject]@{
                    ProcessId = 20
                    ParentProcessId = 10
                    CreationDate = [datetime]"2026-01-01T00:00:02Z"
                }
            }
            10 {
                return [pscustomobject]@{
                    ProcessId = 10
                    ParentProcessId = 0
                    CreationDate = [datetime]"2026-01-01T00:00:01Z"
                }
            }
        }
        return $null
    }

    Assert-True (
        Test-IsProcessDescendantOrSelf `
            -CandidateProcessId 30 `
            -RootProcessId 10
    ) "ancestry accepts a creation-ordered parent chain"
}

& {
    . $LauncherPath -TestOnlyImportFunctions
    $state = [pscustomobject]@{ Kills = 0 }
    function Get-ListeningProcessSnapshots {
        @([pscustomobject]@{
            ProcessId = 42
            StartTimeUtcTicks = 100
        })
    }
    function Get-CimInstance {
        throw "injected CIM query failure"
    }
    function Invoke-ProcessTreeKill {
        param([int]$ProcessId)
        $state.Kills++
    }

    Assert-Throws `
        -MessagePattern "injected CIM query failure" `
        -Message "CIM failure aborts ancestor protection without being hidden" `
        -Action { Stop-PortListeners -LocalPort 61234 }
    Assert-True (
        $state.Kills -eq 0
    ) "CIM failure refused before taskkill"
}

& {
    . $LauncherPath -TestOnlyImportFunctions
    $state = [pscustomobject]@{ Kills = 0 }
    function Get-ListeningProcessSnapshots {
        @([pscustomobject]@{
            ProcessId = 42
            StartTimeUtcTicks = 100
        })
    }
    function Get-LauncherProtectedProcessIds { @(42, 7, 1) }
    function Invoke-ProcessTreeKill {
        param([int]$ProcessId)
        $state.Kills++
    }

    Assert-Throws `
        -MessagePattern "launcher or ancestor" `
        -Message "launcher ancestors are refused before any kill" `
        -Action { Stop-PortListeners -LocalPort 61234 }
    Assert-True ($state.Kills -eq 0) "ancestor refusal did not invoke taskkill"
}

& {
    . $LauncherPath -TestOnlyImportFunctions
    $state = [pscustomobject]@{ Kills = 0 }
    function Get-ListeningProcessSnapshots {
        @([pscustomobject]@{
            ProcessId = 42
            StartTimeUtcTicks = 100
        })
    }
    function Get-LauncherProtectedProcessIds { @(999) }
    function Get-ListeningProcessIds { @() }
    function Invoke-ProcessTreeKill {
        param([int]$ProcessId)
        $state.Kills++
    }

    Stop-PortListeners -LocalPort 61234
    Assert-True ($state.Kills -eq 0) "stale non-owner was skipped"
}

& {
    . $LauncherPath -TestOnlyImportFunctions
    $state = [pscustomobject]@{ Kills = 0 }
    function Get-ListeningProcessSnapshots {
        @([pscustomobject]@{
            ProcessId = 42
            StartTimeUtcTicks = 100
        })
    }
    function Get-LauncherProtectedProcessIds { @(999) }
    function Get-ListeningProcessIds { @(42) }
    function Get-ProcessStartTimeUtcTicks {
        param([int]$ProcessId)
        200
    }
    function Invoke-ProcessTreeKill {
        param([int]$ProcessId)
        $state.Kills++
    }

    Assert-Throws `
        -MessagePattern "reused process ID" `
        -Message "reused PID identity is refused" `
        -Action { Stop-PortListeners -LocalPort 61234 }
    Assert-True ($state.Kills -eq 0) "reused PID refusal did not invoke taskkill"
}

& {
    . $LauncherPath -TestOnlyImportFunctions
    $state = [pscustomobject]@{
        Kills = 0
        FirstTreeWasKilled = $false
    }
    function Get-ListeningProcessSnapshots {
        @(
            [pscustomobject]@{
                ProcessId = 10
                StartTimeUtcTicks = 100
            },
            [pscustomobject]@{
                ProcessId = 11
                StartTimeUtcTicks = 110
            }
        )
    }
    function Get-LauncherProtectedProcessIds { @(999) }
    function Get-ListeningProcessIds {
        if ($state.FirstTreeWasKilled) {
            return @()
        }
        return @(10, 11)
    }
    function Get-ProcessStartTimeUtcTicks {
        param([int]$ProcessId)
        if ($ProcessId -eq 10) { return 100 }
        return 110
    }
    function Invoke-ProcessTreeKill {
        param([int]$ProcessId)
        $state.Kills++
        $state.FirstTreeWasKilled = $true
    }

    Stop-PortListeners -LocalPort 61234
    Assert-True (
        $state.Kills -eq 1
    ) "tree-killed child snapshot is skipped without a false failure"
}

& {
    . $LauncherPath -TestOnlyImportFunctions
    $state = [pscustomobject]@{ Kills = 0 }
    function Get-ListeningProcessSnapshots {
        @([pscustomobject]@{
            ProcessId = 42
            StartTimeUtcTicks = 100
        })
    }
    function Get-LauncherProtectedProcessIds { @(999) }
    function Get-ListeningProcessIds { @(42) }
    function Get-ProcessStartTimeUtcTicks {
        param([int]$ProcessId)
        100
    }
    function Invoke-ProcessTreeKill {
        param([int]$ProcessId)
        $state.Kills++
    }

    Assert-Throws `
        -MessagePattern "supplied together" `
        -Message "expected listener PID requires its start time" `
        -Action {
            Stop-PortListeners `
                -LocalPort 61234 `
                -ExpectedProcessId 42
        }
    Assert-True ($state.Kills -eq 0) "incomplete expected identity did not kill"

    Assert-Throws `
        -MessagePattern "does not match" `
        -Message "expected listener mismatch is refused before any kill" `
        -Action {
            Stop-PortListeners `
                -LocalPort 61234 `
                -ExpectedProcessId 99 `
                -ExpectedStartTimeUtcTicks 200
        }
    Assert-True ($state.Kills -eq 0) "expected identity mismatch did not kill"
}

& {
    . $LauncherPath -TestOnlyImportFunctions
    function Test-ProcessIdentity {
        param(
            [int]$ProcessId,
            [long]$ExpectedStartTimeUtcTicks
        )
        return $true
    }
    function Get-ListeningProcessIds { @(77) }
    function Test-IsProcessDescendantOrSelf {
        param(
            [int]$CandidateProcessId,
            [int]$RootProcessId
        )
        return (
            $CandidateProcessId -eq 77 -and
            $RootProcessId -eq 70
        )
    }
    Assert-True (
        Test-ServerOwnsPort `
            -LocalPort 61234 `
            -ServerProcessId 70 `
            -ServerStartTimeUtcTicks 700
    ) "readiness accepts a descendant listener with stable root identity"

    function Test-ProcessIdentity {
        param(
            [int]$ProcessId,
            [long]$ExpectedStartTimeUtcTicks
        )
        return $false
    }
    Assert-True (
        -not (Test-ServerOwnsPort `
            -LocalPort 61234 `
            -ServerProcessId 70 `
            -ServerStartTimeUtcTicks 700)
    ) "readiness refuses a reused server root identity"
}

& {
    . $LauncherPath -TestOnlyImportFunctions

    $dummyRootProcess = $null
    $dummyRootStartTimeUtcTicks = 0
    $dummyListenerProcess = $null
    $dummyListenerStartTimeUtcTicks = 0
    $earlyExitProcess = $null
    $earlyExitStartTimeUtcTicks = 0
    $testOwnsPort = $false
    $earlyExitSuffix = (
        "$EarlyExitPort-$([Guid]::NewGuid().ToString('N'))"
    )
    $earlyExitStdoutPath = Join-Path `
        $env:TEMP `
        "web-keyword-early-exit-$earlyExitSuffix.stdout.log"
    $earlyExitStderrPath = Join-Path `
        $env:TEMP `
        "web-keyword-early-exit-$earlyExitSuffix.stderr.log"

    try {
        $venvPython = Join-Path `
            $ProjectRoot `
            ".venv\Scripts\python.exe"
        $python = if (Test-Path -LiteralPath $venvPython) {
            $venvPython
        } else {
            (Get-Command python.exe -ErrorAction Stop).Source
        }
        $dummyRootProcess = Start-Process `
            -FilePath $python `
            -ArgumentList @(
                "-m", "http.server", [string]$EarlyExitPort,
                "--bind", "127.0.0.1"
            ) `
            -WindowStyle Hidden `
            -PassThru
        $null = $dummyRootProcess.Handle
        $dummyRootStartTimeUtcTicks = [long](
            $dummyRootProcess.StartTime.ToUniversalTime().Ticks
        )

        $dummyDeadline = (Get-Date).AddSeconds(10)
        do {
            $dummyRootProcess.Refresh()
            if ($dummyRootProcess.HasExited) {
                throw "Controlled dummy listener exited before binding."
            }

            $listeners = @(Get-NetTCPConnection `
                -LocalPort $EarlyExitPort `
                -State Listen `
                -ErrorAction SilentlyContinue)
            if ($listeners.Count -eq 1 -and
                    (Test-IsProcessDescendantOrSelf `
                        -CandidateProcessId $listeners[0].OwningProcess `
                        -RootProcessId $dummyRootProcess.Id)) {
                $candidateListenerProcess = Get-Process `
                    -Id $listeners[0].OwningProcess `
                    -ErrorAction SilentlyContinue
                if ($null -ne $candidateListenerProcess) {
                    $candidateStartTimeUtcTicks = [long](
                        $candidateListenerProcess.StartTime.
                            ToUniversalTime().Ticks
                    )
                    if (Test-ProcessIdentity `
                            -ProcessId $candidateListenerProcess.Id `
                            -ExpectedStartTimeUtcTicks `
                                $candidateStartTimeUtcTicks) {
                        $dummyListenerProcess = $candidateListenerProcess
                        $dummyListenerStartTimeUtcTicks = (
                            $candidateStartTimeUtcTicks
                        )
                        break
                    }
                }
            }
            Start-Sleep -Milliseconds 100
        } while ((Get-Date) -lt $dummyDeadline)

        Assert-True (
            $null -ne $dummyListenerProcess
        ) "controlled dummy listener owns the early-exit test port"
        $testOwnsPort = $true

        $powershellExecutable = Join-Path $PSHOME "powershell.exe"
        $earlyExitArguments = (
            "-NoProfile -ExecutionPolicy Bypass " +
            "-File `"$LauncherPath`" " +
            "-Port $EarlyExitPort -NoBrowser " +
            "-ReadyTimeoutSeconds 5 -TestInvalidServerArgument " +
            "-ExpectedListenerProcessId $($dummyListenerProcess.Id) " +
            "-ExpectedListenerStartTimeUtcTicks " +
                "$dummyListenerStartTimeUtcTicks"
        )
        $earlyExitProcess = Start-Process `
            -FilePath $powershellExecutable `
            -ArgumentList $earlyExitArguments `
            -WorkingDirectory $ProjectRoot `
            -RedirectStandardOutput $earlyExitStdoutPath `
            -RedirectStandardError $earlyExitStderrPath `
            -PassThru
        $null = $earlyExitProcess.Handle
        $earlyExitStartTimeUtcTicks = [long](
            $earlyExitProcess.StartTime.ToUniversalTime().Ticks
        )
        Assert-True (
            $earlyExitProcess.WaitForExit(15000)
        ) "invalid server argument exits promptly"
        $earlyExitProcess.Refresh()

        $earlyExitStderr = Get-Content `
            -Raw `
            -LiteralPath $earlyExitStderrPath
        Assert-True (
            $earlyExitProcess.ExitCode -eq 2
        ) "invalid server argument propagates exact exit code 2"
        Assert-True (
            $earlyExitStderr -match "exit code 2"
        ) "invalid server argument writes exit code 2 diagnostic to stderr"
        $dummyListenerProcess.Refresh()
        Assert-True (
            $dummyListenerProcess.HasExited
        ) "launcher stopped the controlled dummy listener"
        Assert-True (
            @(Get-NetTCPConnection `
                -LocalPort $EarlyExitPort `
                -State Listen `
                -ErrorAction SilentlyContinue).Count -eq 0
        ) "early-exit test port has no listener after exact exit 2"
    } finally {
        if ($null -ne $earlyExitProcess) {
            Stop-LaunchedServerProcess `
                -Process $earlyExitProcess `
                -ExpectedStartTimeUtcTicks $earlyExitStartTimeUtcTicks
        }
        if ($null -ne $dummyListenerProcess) {
            Stop-LaunchedServerProcess `
                -Process $dummyListenerProcess `
                -ExpectedStartTimeUtcTicks `
                    $dummyListenerStartTimeUtcTicks
        }
        if ($null -ne $dummyRootProcess) {
            Stop-LaunchedServerProcess `
                -Process $dummyRootProcess `
                -ExpectedStartTimeUtcTicks `
                    $dummyRootStartTimeUtcTicks
        }

        try {
            if ($testOwnsPort) {
                $cleanupDeadline = (Get-Date).AddSeconds(10)
                do {
                    if (@(Get-NetTCPConnection `
                            -LocalPort $EarlyExitPort `
                            -State Listen `
                            -ErrorAction SilentlyContinue).Count -eq 0) {
                        break
                    }
                    Start-Sleep -Milliseconds 100
                } while ((Get-Date) -lt $cleanupDeadline)
                Assert-True (
                    @(Get-NetTCPConnection `
                        -LocalPort $EarlyExitPort `
                        -State Listen `
                        -ErrorAction SilentlyContinue).Count -eq 0
                ) "owned early-exit test port is free after cleanup"
            }
        } finally {
            Remove-Item `
                -LiteralPath (
                    $earlyExitStdoutPath,
                    $earlyExitStderrPath
                ) `
                -Force `
                -ErrorAction SilentlyContinue
        }
    }
}

Write-Host "Debug launcher safety tests passed."
