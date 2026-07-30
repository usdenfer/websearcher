# 一键调试启动器设计

日期：2026-07-30

## 背景

项目在 Windows 上以 `uvicorn` reload 模式运行时会创建额外 worker。父进程被终止后，worker 可能继续占用端口并提供旧代码。已有 Kimi 会话为此加入了 `server.py --no-reload`，并尝试用纯 Batch 脚本完成端口清理、服务启动和浏览器打开，但该脚本受到 Batch 管道转义、中文编码与异常行尾影响，尚不能可靠使用。

## 目标

提供两个一致的一键调试入口：

- 在资源管理器中双击 `start_debug.bat`。
- 在 IDE 终端或 npm 运行配置中执行 `npm run dev`。

两个入口都必须：

1. 默认监听 `127.0.0.1:7100`。
2. 支持指定其他端口。
3. 启动前找到并结束占用目标端口的进程树。
4. 优先使用项目 `.venv\Scripts\python.exe`，不存在时回退到 `python`。
5. 使用 `server.py --no-reload` 启动单进程服务。
6. 等待服务可访问后打开浏览器。
7. 在启动失败时显示清晰错误，并返回非零退出码。
8. 不结束与目标端口无关的 Python 或 Node 进程。

## 方案

### 统一的 PowerShell 核心

新增 `scripts/start-debug.ps1`，负责所有实际逻辑：

- 接收 `-HostName`、`-Port` 和可选的 `-NoBrowser` 参数。
- 校验端口范围。
- 通过 `Get-NetTCPConnection` 查找目标端口监听者。
- 对每个监听者执行 `taskkill /PID <pid> /T /F`，然后确认端口已释放。
- 选择 Python 解释器并检查其可执行性。
- 启动后台就绪探针；探针确认 HTTP 服务可访问后打开浏览器。
- 在当前控制台前台执行：

  ```text
  python server.py --no-reload --host <host> --port <port>
  ```

服务保持前台运行，因此日志和异常会直接显示在双击窗口或 IDE 控制台中。`--no-reload` 保证服务本身不再派生 reload worker。

### 双击入口

`start_debug.bat` 仅完成三件事：

1. 切换到仓库根目录。
2. 调用 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\start-debug.ps1`。
3. 在失败时保留窗口并显示退出码。

Batch 文件只使用 ASCII 文本，不包含管道、复杂括号块或中文注释，避免再次触发编码和解析问题。

命令行可用法：

```text
start_debug.bat
start_debug.bat 7200
```

### IDE/npm 入口

`package.json` 的现有 `dev` 命令改为：

```json
"dev": "powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/start-debug.ps1"
```

常用方式：

```text
npm run dev
npm.cmd run dev -- -Port 7200
npm.cmd run dev -- -NoBrowser
```

npm 与 Batch 不复制启动逻辑，后续修复只需修改一个 PowerShell 文件。
不新增 `npm run debug`。原 `scripts/dev.mjs` 不再作为 npm 默认入口；如果确认没有其他调用者，实施时将删除该冗余启动器。

## 错误处理

- 非法端口：启动前直接失败。
- 目标端口进程无法结束：报告 PID 并停止，不尝试双重绑定。
- 找不到 Python：提示创建 `.venv` 或安装 Python。
- `server.py` 退出：透传其退出码。
- 就绪探针超时：输出警告但不杀死仍在运行的服务，便于查看启动日志。
- 浏览器启动失败：输出警告，不影响服务运行。

## 验证

新增一个 PowerShell 测试脚本或等价自动化检查，覆盖：

1. `npm run dev` 确实进入包含 `--no-reload` 的统一启动路径。
2. 默认参数解析为 `127.0.0.1:7100`。
3. 自定义端口能够透传。
4. 被占用测试端口的进程树会被结束。
5. 真服务启动后 HTTP 可访问。
6. 测试结束后端口无监听者，且没有遗留本次启动的服务进程。
7. `start_debug.bat` 和 `npm run dev` 都调用同一 PowerShell 核心。

测试只使用专用临时端口，不触碰当前可能正在运行的 `7100` 服务。

## 范围

本次只实现 Windows 本地调试启动器，不修改生产启动方式。`npm run dev` 将从 reload 模式改为统一的 `--no-reload` 单进程启动方式。不会重构 Kimi 会话产生的其他业务代码改动。未完成的 `start.bat`、`portcheck_test.bat` 等临时文件将在实现时先确认用途，再仅清理可明确归属于这次失败尝试的文件。
