// Dev launcher: forward --host/--port CLI args to the Python server.
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";

const args = process.argv.slice(2);
let host = "127.0.0.1";
let port = "7100";
for (let i = 0; i < args.length; i++) {
  const a = args[i];
  if (a === "--host" && args[i + 1]) host = args[++i];
  else if (a.startsWith("--host=")) host = a.slice(7);
  else if (a === "--port" && args[i + 1]) port = args[++i];
  else if (a.startsWith("--port=")) port = a.slice(7);
}

const projectRoot = fileURLToPath(new URL("..", import.meta.url));
// 优先使用项目 .venv（含 playwright 等依赖），不存在则回退系统 python
const venvPython = new URL("../.venv/Scripts/python.exe", import.meta.url);
let python = process.env.PYTHON || "python";
try {
  const p = fileURLToPath(venvPython);
  if (existsSync(p)) python = p;
} catch {}
const child = spawn(python, ["server.py", "--host", host, "--port", port], {
  stdio: "inherit",
  cwd: projectRoot,
});

child.on("error", (err) => {
  console.error("无法启动 Python 服务：", err.message);
  process.exit(1);
});
child.on("exit", (code) => process.exit(code ?? 0));

for (const sig of ["SIGINT", "SIGTERM"]) {
  process.on(sig, () => child.kill(sig));
}
