[CmdletBinding()]
param(
    [ValidateRange(1024, 65535)]
    [int]$Port = 47123
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot

function Assert-True {
    param(
        [bool]$Condition,
        [string]$Message
    )

    if (-not $Condition) {
        throw "Assertion failed: $Message"
    }
}

$launcherPath = Join-Path $ProjectRoot "scripts\start-debug.ps1"
$batchPath = Join-Path $ProjectRoot "start_debug.bat"
$packagePath = Join-Path $ProjectRoot "package.json"
$serverPath = Join-Path $ProjectRoot "server.py"
$readmePath = Join-Path $ProjectRoot "README.md"
$designPath = Join-Path $ProjectRoot "docs\superpowers\specs\2026-07-30-one-click-debug-launcher-design.md"
$planPath = Join-Path $ProjectRoot "docs\superpowers\plans\2026-07-30-one-click-debug-launcher.md"

Assert-True (Test-Path -LiteralPath $launcherPath) "PowerShell launcher exists"
Assert-True (Test-Path -LiteralPath $batchPath) "Batch launcher exists"

$package = Get-Content -Raw -Encoding UTF8 $packagePath | ConvertFrom-Json
Assert-True (
    $package.scripts.dev -eq
        "powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/start-debug.ps1"
) "npm run dev uses the shared PowerShell launcher"

$batch = Get-Content -Raw -Encoding ASCII $batchPath
Assert-True (
    $batch -match "scripts\\start-debug\.ps1"
) "Batch launcher uses the shared PowerShell launcher"

$launcher = Get-Content -Raw -Encoding UTF8 $launcherPath
$tokens = $null
$parseErrors = $null
$launcherAst = [System.Management.Automation.Language.Parser]::ParseFile(
    $launcherPath,
    [ref]$tokens,
    [ref]$parseErrors
)
Assert-True ($parseErrors.Count -eq 0) "launcher parses successfully"
Assert-True (
    $launcherAst.ParamBlock.Parameters[0].Name.VariablePath.UserPath -eq "Port"
) "launcher binds a surviving npm positional argument to Port"
Assert-True ($launcher -match '"--no-reload"') "launcher always supplies --no-reload"
Assert-True ($launcher -match "Get-NetTCPConnection") "launcher checks the target port"
Assert-True ($launcher -match "taskkill\.exe") "launcher terminates the listener tree"

$server = Get-Content -Raw -Encoding UTF8 $serverPath
Assert-True ($server -match '"--no-reload"') "server accepts --no-reload"

$launchDocs = @(
    @{ Name = "README"; Content = Get-Content -Raw -Encoding UTF8 $readmePath },
    @{ Name = "launcher design"; Content = Get-Content -Raw -Encoding UTF8 $designPath },
    @{ Name = "launcher plan"; Content = Get-Content -Raw -Encoding UTF8 $planPath }
)
$readme = $launchDocs[0].Content
Assert-True ($readme -match "npm run dev") "README keeps the default npm launcher command"
Assert-True (
    ($launchDocs.Content -join "`n") -match "npm\.cmd run dev -- -Port"
) "launcher docs include npm.cmd for named launcher options"
foreach ($launchDoc in $launchDocs) {
    Assert-True (
        $launchDoc.Content -notmatch "npm run dev -- -Port"
    ) "$($launchDoc.Name) does not advertise unsafe PowerShell npm Port arguments"
    Assert-True (
        $launchDoc.Content -notmatch "npm run dev -- -NoBrowser"
    ) "$($launchDoc.Name) does not advertise unsafe PowerShell npm NoBrowser arguments"
}

Write-Host "Static launcher contract passed."
