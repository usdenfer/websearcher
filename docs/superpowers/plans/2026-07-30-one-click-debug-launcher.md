# One-Click Debug Launcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `start_debug.bat` and `npm run dev` launch the Windows development server through one reliable `--no-reload` workflow that clears the selected port and opens the browser after readiness.

**Architecture:** A single PowerShell launcher owns argument validation, target-port cleanup, Python selection, service process lifetime, readiness polling, and optional browser launch. The Batch and npm entrypoints remain thin adapters to that launcher, so behavior cannot drift between Explorer and IDE usage.

**Tech Stack:** Windows PowerShell 5.1+, Batch, npm/package.json, Python/FastAPI/uvicorn, PowerShell integration tests.

---

## File Structure

- Create `scripts/start-debug.ps1`: the only implementation of the debug launch workflow.
- Replace `start_debug.bat`: ASCII-only double-click adapter; positional first argument is the port.
- Modify `package.json`: route the existing `npm run dev` command to the PowerShell launcher.
- Modify `server.py`: retain and verify the existing uncommitted `--no-reload` CLI support.
- Create `tests/test_start_debug.ps1`: dependency-free contract and end-to-end Windows test.
- Modify `README.md`: document double-click, npm, custom-port, and no-browser usage.
- Delete `scripts/dev.mjs`: obsolete Node launcher after `npm run dev` uses PowerShell.
- Delete `start.bat` and `portcheck_test.bat`: failed Kimi-session artifacts superseded by the tested launcher.

### Task 1: Add the Failing Launcher Contract Test

**Files:**
- Create: `tests/test_start_debug.ps1`
- Inspect: `package.json`
- Inspect: `server.py`
- Inspect: `start_debug.bat`

- [ ] **Step 1: Write the dependency-free contract test**

Create `tests/test_start_debug.ps1` with the following initial content:

```powershell
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
Assert-True ($launcher -match '"--no-reload"') "launcher always supplies --no-reload"
Assert-True ($launcher -match "Get-NetTCPConnection") "launcher checks the target port"
Assert-True ($launcher -match "taskkill\.exe") "launcher terminates the listener tree"

$server = Get-Content -Raw -Encoding UTF8 $serverPath
Assert-True ($server -match '"--no-reload"') "server accepts --no-reload"

Write-Host "Static launcher contract passed."
```

- [ ] **Step 2: Run the test and verify that it fails before implementation**

Run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tests/test_start_debug.ps1
```

Expected: FAIL with `Assertion failed: PowerShell launcher exists`, because `scripts/start-debug.ps1` has not been created.

- [ ] **Step 3: Commit only the failing test**

Run:

```powershell
git add -- tests/test_start_debug.ps1
git commit -m "test: define debug launcher contract"
```

Expected: one commit containing only `tests/test_start_debug.ps1`; existing discovery changes remain unstaged.

### Task 2: Implement the Shared PowerShell Launcher

**Files:**
- Create: `scripts/start-debug.ps1`
- Modify: `server.py:479-496`

- [ ] **Step 1: Create the minimal launcher implementation**

Create `scripts/start-debug.ps1`:

```powershell
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

    @(
        Get-NetTCPConnection `
            -LocalPort $LocalPort `
            -State Listen `
            -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique |
            Where-Object { $_ -gt 0 }
    )
}

function Stop-PortListeners {
    param([int]$LocalPort)

    $listenerPids = @(Get-ListeningProcessIds -LocalPort $LocalPort)
    foreach ($listenerPid in $listenerPids) {
        if ($listenerPid -eq $PID) {
            throw "Refusing to stop launcher process $PID on port $LocalPort."
        }

        Write-Host "Stopping process tree $listenerPid on port $LocalPort..."
        & "$env:SystemRoot\System32\taskkill.exe" `
            /PID $listenerPid /T /F |
            Out-Host
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to stop process tree $listenerPid on port $LocalPort."
        }
    }

    $deadline = (Get-Date).AddSeconds(5)
    while ((Get-ListeningProcessIds -LocalPort $LocalPort).Count -gt 0) {
        if ((Get-Date) -ge $deadline) {
            $remaining = (
                Get-ListeningProcessIds -LocalPort $LocalPort
            ) -join ", "
            throw "Port $LocalPort is still owned by process(es): $remaining"
        }
        Start-Sleep -Milliseconds 100
    }
}

function Resolve-ProjectPython {
    $venvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython) {
        return $venvPython
    }

    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($null -eq $pythonCommand) {
        throw "Python was not found. Create .venv or install Python."
    }
    return $pythonCommand.Source
}

Set-Location -LiteralPath $ProjectRoot
Stop-PortListeners -LocalPort $Port
$python = Resolve-ProjectPython

Write-Host "Starting debug server at $Url"
Write-Host "Mode: single process (--no-reload)"

$serverProcess = Start-Process `
    -FilePath $python `
    -ArgumentList @(
        "server.py",
        "--no-reload",
        "--host", $HostName,
        "--port", [string]$Port
    ) `
    -WorkingDirectory $ProjectRoot `
    -NoNewWindow `
    -PassThru

try {
    $deadline = (Get-Date).AddSeconds($ReadyTimeoutSeconds)
    $ready = $false
    while (-not $serverProcess.HasExited -and (Get-Date) -lt $deadline) {
        try {
            $response = Invoke-WebRequest `
                -UseBasicParsing `
                -Uri $Url `
                -TimeoutSec 2
            if ($response.StatusCode -eq 200) {
                $ready = $true
                break
            }
        }
        catch {
            Start-Sleep -Milliseconds 250
        }
    }

    if ($serverProcess.HasExited) {
        throw "Server exited before becoming ready (exit code $($serverProcess.ExitCode))."
    }

    if ($ready) {
        Write-Host "Server is ready: $Url"
        if (-not $NoBrowser) {
            try {
                Start-Process $Url
            }
            catch {
                Write-Warning "Browser could not be opened: $($_.Exception.Message)"
            }
        }
    }
    else {
        Write-Warning (
            "Server did not become ready within $ReadyTimeoutSeconds seconds. " +
            "It is still running so startup logs can be inspected."
        )
    }

    $serverProcess.WaitForExit()
    exit $serverProcess.ExitCode
}
finally {
    if ($null -ne $serverProcess -and -not $serverProcess.HasExited) {
        & "$env:SystemRoot\System32\taskkill.exe" `
            /PID $serverProcess.Id /T /F |
            Out-Null
    }
}
```

- [ ] **Step 2: Retain the existing `server.py --no-reload` implementation**

Confirm that the current uncommitted `server.py` block remains exactly:

```python
parser.add_argument(
    "--no-reload",
    action="store_true",
    help="单进程运行（不做文件变更自动重启），停止时不留 worker 进程",
)
args = parser.parse_args()
uvicorn.run("server:app", host=args.host, port=args.port,
            reload=not args.no_reload,
            loop="server:proactor_loop_factory")
```

Do not alter any unrelated server behavior.

- [ ] **Step 3: Run the contract test and observe the next expected failure**

Run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tests/test_start_debug.ps1
```

Expected: FAIL at `npm run dev uses the shared PowerShell launcher`, because `package.json` still points to `scripts/dev.mjs`.

- [ ] **Step 4: Run Python CLI validation**

Run:

```powershell
.\.venv\Scripts\python.exe server.py --help
```

Expected: exit code 0 and help output includes `--no-reload`.

- [ ] **Step 5: Commit the launcher and CLI flag**

Run:

```powershell
git add -- scripts/start-debug.ps1 server.py
git commit -m "feat: add no-reload PowerShell launcher"
```

Expected: only `scripts/start-debug.ps1` and the relevant `server.py` change are committed.

### Task 3: Connect the Batch and npm Entrypoints

**Files:**
- Replace: `start_debug.bat`
- Modify: `package.json`
- Delete: `scripts/dev.mjs`

- [ ] **Step 1: Replace the failed Batch experiment with an ASCII-only adapter**

Replace `start_debug.bat` with:

```bat
@echo off
setlocal
cd /d "%~dp0"

if "%~1"=="" (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\start-debug.ps1"
) else (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\start-debug.ps1" -Port "%~1"
)

set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo.
    echo Debug launcher failed with exit code %EXIT_CODE%.
    pause
)
exit /b %EXIT_CODE%
```

- [ ] **Step 2: Point the existing npm development command at the shared launcher**

Change `package.json` scripts to:

```json
"scripts": {
  "dev": "powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/start-debug.ps1"
}
```

Do not add a `debug` script.

- [ ] **Step 3: Remove the now-unreferenced Node launcher**

Verify no tracked file except `package.json` references it:

```powershell
rg -n "scripts/dev\.mjs|dev\.mjs" --glob "!docs/superpowers/**"
```

Expected before changing `package.json`: only `package.json` references `scripts/dev.mjs`.

After `package.json` is updated, delete `scripts/dev.mjs`.

- [ ] **Step 4: Run the contract test**

Run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tests/test_start_debug.ps1
```

Expected: `Static launcher contract passed.`

- [ ] **Step 5: Commit the entrypoints**

Run:

```powershell
git add -- start_debug.bat package.json scripts/dev.mjs
git commit -m "feat: unify Windows development entrypoints"
```

Expected: the commit replaces the broken Batch content, updates `npm run dev`, and removes `scripts/dev.mjs`.

### Task 4: Add the End-to-End Port Replacement Test

**Files:**
- Modify: `tests/test_start_debug.ps1`

- [ ] **Step 1: Append the integration scenario**

Append this code after `Write-Host "Static launcher contract passed."`:

```powershell
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
            /PID $Process.Id /T /F |
            Out-Null
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
        -FilePath "powershell.exe" `
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
```

- [ ] **Step 2: Run the integration test**

Run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tests/test_start_debug.ps1 -Port 47123
```

Expected output:

```text
Static launcher contract passed.
Port replacement integration passed.
All debug launcher tests passed.
```

Expected cleanup: `Get-NetTCPConnection -LocalPort 47123 -State Listen` returns no listener after the test.

- [ ] **Step 3: Diagnose any failure without weakening assertions**

If the test fails, inspect:

```powershell
Get-Content "$env:TEMP\web-keyword-debug-47123.stdout.log"
Get-Content "$env:TEMP\web-keyword-debug-47123.stderr.log"
Get-NetTCPConnection -LocalPort 47123 -State Listen -ErrorAction SilentlyContinue
```

Fix the launcher, rerun the exact integration test, and retain all assertions.

- [ ] **Step 4: Commit the integration coverage**

Run:

```powershell
git add -- tests/test_start_debug.ps1 scripts/start-debug.ps1
git commit -m "test: verify debug launcher replaces port listener"
```

Expected: test and any necessary launcher correction are committed; no unrelated files are staged.

### Task 5: Document Usage and Remove Failed Artifacts

**Files:**
- Modify: `README.md:55-80`
- Delete: `start.bat`
- Delete: `portcheck_test.bat`

- [ ] **Step 1: Replace the old reload guidance**

Replace the existing development-start and reload paragraphs with:

```markdown
# 3. 启动开发服务
npm run dev
# 默认打开 http://127.0.0.1:7100
```

不配置 AI key 时，正文搜索仍可正常使用，AI 功能会降级。

Windows 开发启动器会先结束占用目标端口的进程树，再以
`--no-reload` 单进程模式启动服务，服务就绪后自动打开浏览器。
这样关闭 IDE 任务或启动窗口时不会遗留 uvicorn reload worker。

也可以在资源管理器中双击 `start_debug.bat`。自定义端口或禁止
自动打开浏览器：

```powershell
npm run dev -- -Port 7200
npm run dev -- -NoBrowser
start_debug.bat 7200
```

启动器只会清理目标端口的监听进程，不会结束其他 Python 或 Node
服务。
```

- [ ] **Step 2: Delete only the confirmed failed artifacts**

Delete:

```text
start.bat
portcheck_test.bat
```

Keep `start_debug.bat`, which is now the tested double-click entrypoint.

- [ ] **Step 3: Run documentation and launcher checks**

Run:

```powershell
rg -n "npm run debug|npm run dev -- --no-reload|portcheck_test|start\.bat" README.md package.json start_debug.bat
```

Expected: no matches.

Run:

```powershell
git diff --check
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tests/test_start_debug.ps1 -Port 47124
```

Expected: `git diff --check` exits 0; all launcher tests pass on port `47124`.

- [ ] **Step 4: Commit documentation and artifact cleanup**

Run:

```powershell
git add -- README.md start.bat portcheck_test.bat
git commit -m "docs: explain one-click development startup"
```

Expected: only README and the two failed artifacts are included.

### Task 6: Final Verification

**Files:**
- Verify: `scripts/start-debug.ps1`
- Verify: `start_debug.bat`
- Verify: `package.json`
- Verify: `server.py`
- Verify: `tests/test_start_debug.ps1`
- Verify: `README.md`

- [ ] **Step 1: Invoke the verification-before-completion skill**

Read and follow `superpowers:verification-before-completion` before making any success claim.

- [ ] **Step 2: Run the launcher integration test on a fresh port**

Run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tests/test_start_debug.ps1 -Port 47125
```

Expected: all three success messages and exit code 0.

- [ ] **Step 3: Run the existing Python suite**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: all tests pass with no failures.

- [ ] **Step 4: Verify no test listener remains**

Run:

```powershell
Get-NetTCPConnection -LocalPort 47123,47124,47125 -State Listen -ErrorAction SilentlyContinue
```

Expected: no output.

- [ ] **Step 5: Verify exact diff scope**

Run:

```powershell
git status --short
git log --oneline -6
```

Expected: launcher commits are present. Pre-existing discovery adapter/provider/engine changes may remain unstaged, but no launcher test process or temporary file remains.

- [ ] **Step 6: Report the two supported commands**

Report:

```text
npm run dev
start_debug.bat
```

Also report custom port syntax and the fresh verification results without claiming unrelated dirty-worktree changes were completed.
