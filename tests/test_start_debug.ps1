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

$readme = Get-Content -Raw -Encoding UTF8 $readmePath
Assert-True ($readme -match "npm run dev") "README keeps the default npm launcher command"
$markdownDocs = @(Get-ChildItem -LiteralPath $ProjectRoot -Recurse -File -Filter "*.md")
$markdownContent = @($markdownDocs | ForEach-Object {
    Get-Content -Raw -Encoding UTF8 $_.FullName
})
Assert-True (
    ($markdownContent -join "`n") -match "npm\.cmd run dev -- -(Port|Host|NoBrowser)"
) "repository Markdown includes npm.cmd for named launcher options"
$unsafeNpmOptionPattern = "(?im)npm\s+run\s+dev(?:\s|\[)*--\s*(?:-Port|--port|-Host|--host|-NoBrowser)\b"
for ($index = 0; $index -lt $markdownDocs.Count; $index++) {
    Assert-True (
        $markdownContent[$index] -notmatch $unsafeNpmOptionPattern
    ) "$($markdownDocs[$index].FullName) does not advertise unsafe PowerShell npm named arguments"
}

Write-Host "Static launcher contract passed."

function Wait-Until {
    param(
        [scriptblock]$Condition,
        [int]$TimeoutSeconds,
        [string]$FailureMessage
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (& $Condition) {
            return
        }
        Start-Sleep -Milliseconds 200
    }
    throw $FailureMessage
}

function Stop-ProcessTree {
    param([System.Diagnostics.Process]$Process)

    if ($null -ne $Process -and -not $Process.HasExited) {
        & "$env:SystemRoot\System32\taskkill.exe" `
            /PID $Process.Id /T /F `
            *> $null
    }
}

function Get-TestPython {
    $venvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython) {
        return $venvPython
    }
    return (Get-Command python.exe -ErrorAction Stop).Source
}

$dummy = $null
$launcherProcess = $null
$stdoutPath = Join-Path $env:TEMP "web-keyword-debug-$Port.stdout.log"
$stderrPath = Join-Path $env:TEMP "web-keyword-debug-$Port.stderr.log"

try {
    if (
        Get-NetTCPConnection `
            -LocalPort $Port `
            -State Listen `
            -ErrorAction SilentlyContinue
    ) {
        throw "Integration-test port $Port is already in use."
    }

    $python = Get-TestPython
    $dummy = Start-Process `
        -FilePath $python `
        -ArgumentList @(
            "-m", "http.server", [string]$Port,
            "--bind", "127.0.0.1"
        ) `
        -WindowStyle Hidden `
        -PassThru

    Wait-Until `
        -TimeoutSeconds 10 `
        -FailureMessage "Dummy listener did not start on port $Port." `
        -Condition {
            $null -ne (
                Get-NetTCPConnection `
                    -LocalPort $Port `
                    -State Listen `
                    -ErrorAction SilentlyContinue
            )
        }

    $launcherProcess = Start-Process `
        -FilePath "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" `
        -ArgumentList @(
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-File", $launcherPath,
            "-Port", [string]$Port,
            "-NoBrowser"
        ) `
        -WorkingDirectory $ProjectRoot `
        -RedirectStandardOutput $stdoutPath `
        -RedirectStandardError $stderrPath `
        -PassThru

    Wait-Until `
        -TimeoutSeconds 30 `
        -FailureMessage "FastAPI server did not replace the dummy listener." `
        -Condition {
            try {
                $response = Invoke-WebRequest `
                    -UseBasicParsing `
                    -Uri "http://127.0.0.1:$Port/" `
                    -TimeoutSec 2
                return (
                    $response.StatusCode -eq 200 -and
                    $response.Content -match "站内关键词搜索"
                )
            }
            catch {
                return $false
            }
        }

    $dummy.Refresh()
    Assert-True $dummy.HasExited "existing port listener was terminated"
    Assert-True (-not $launcherProcess.HasExited) "launcher remains active with server"
    Write-Host "Port replacement integration passed."
}
finally {
    Stop-ProcessTree -Process $launcherProcess
    Stop-ProcessTree -Process $dummy

    Wait-Until `
        -TimeoutSeconds 10 `
        -FailureMessage "Integration-test port $Port remained occupied." `
        -Condition {
            $null -eq (
                Get-NetTCPConnection `
                    -LocalPort $Port `
                    -State Listen `
                    -ErrorAction SilentlyContinue
            )
        }
}

Write-Host "All debug launcher tests passed."
