[CmdletBinding()]
param()

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
        "bad_name.example"
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

Write-Host "Debug launcher safety tests passed."
