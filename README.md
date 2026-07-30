# 站内正文关键词搜索工具

输入网站入口和关键词，工具会从普通链接与网站公开的结构化入口发现候选页，
抓取候选页后仅以正文可见文本确认命中。结构化发现不受普通 BFS 的点击深度
限制，因此深层旧文章也有机会在深度 1～3 下被发现；它仍受统一时间和页面
预算约束，不承诺遍历大型网站的全部历史内容。

## 核心能力

- **正文专用匹配**：优先提取 `article`、`main` 等正文容器，过滤导航、页脚、
  侧栏和隐藏内容。标题、URL、元数据、搜索结果摘要只参与展示或候选排序，
  不能单独产生命中。
- **多通道发现**：综合基础 BFS、公开搜索表单、sitemap、Feed 和栏目分页；
  候选 URL 会统一规范化、去重、排序，再抓取正文验证。
- **静态、渲染与自动模式**：
  - 自动（默认）：先静态抓取；零命中或疑似动态站且命中较少时尝试渲染补搜。
  - 强制 JS 渲染：用 Playwright 获取动态入口和链接，正文仍优先静态抓取。
  - 仅静态：只使用 HTTP 抓取，适合普通静态网站。
- **可选 CMS 适配器**：当前仅为 FreeCMS（中央政府采购网）和云南政府站
  CMS 保留定向适配；其他网站走通用探测。Gamersky 只是通用规则实验站，
  没有专用适配器。
- **可解释诊断**：响应中的 `discovery` 包含使用的 profile、尝试/成功来源、
  候选数、正文抓取数、预算是否扩展、是否部分完成、耗时和脱敏警告。
- **AI 助手**（可选 DeepSeek key）：支持关键词扩展、结果摘要和基于本次
  抓取正文的问答。搜索结果缓存 10 分钟；缓存 TTL 不等于搜索执行预算。
- **定时搜索任务**：支持每天定时或按小时运行，以命中指纹识别新增结果。
  数据保存在已忽略的 `data/jobs.json`。

## 时间与页面预算

一次搜索共享 **120 秒**截止时间。基础 BFS 预留最多 **30 页**，随后结构化
发现与候选正文抓取使用统一 HTML 页面预算：

- 初始总预算为 60 页，基础页、列表页、渲染页和正文页都计入；
- 仍有高价值候选时可扩展到最多 120 页；
- 达到时间或页面上限后立即返回已有结果，并标记
  `discovery.partial: true`；
- sitemap、Feed、CMS API 等非 HTML 请求另有各 Provider 的小额配额。

页面预算是上限而非成功页计数：失败请求也可能消耗一次尝试，因此
`candidatesFetched` 可以小于实际请求数。

## 快速开始

```powershell
# 1. 创建虚拟环境并安装依赖
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install playwright fastapi "uvicorn[standard]" httpx pydantic python-dotenv pytest beautifulsoup4
.\.venv\Scripts\python.exe -m playwright install chromium

# 2. 配置可选 AI key
Copy-Item .env.example .env
# 编辑 .env，填写 DEEPSEEK_API_KEY

# 3. 启动开发服务
npm run dev
# 默认 http://127.0.0.1:7100
```

`npm run dev` 是默认的一键启动方式；它默认在 `http://127.0.0.1:7100`
启动并打开浏览器。Windows 启动器会先结束所选目标端口上正在监听的进程树，
再以 `--no-reload` 单进程模式启动服务、等待就绪并打开浏览器，从而避免
uvicorn reload worker 残留。

也可以在资源管理器中双击 `start_debug.bat` 启动。要指定端口，请使用：

```powershell
.\start_debug.bat 7200
```

在 PowerShell 或 IDE 中使用自定义选项时，请显式使用 `npm.cmd`，不要使用
`npm.ps1`；后者会剥离 PowerShell 风格的命名参数。安全的写法包括：

```powershell
npm.cmd run dev -- -Port 7200
npm.cmd run dev -- -NoBrowser
npm.cmd run dev -- -Host 0.0.0.0 -Port 7200
```

启动器只会终止所选目标端口的监听进程，不会影响其他 Python 或 Node 服务。

不配置 AI key 时，正文搜索仍可正常使用，AI 功能会降级。

## API 兼容性

`POST /api/search` 请求继续使用：

```json
{
  "startUrl": "https://example.com/",
  "keywords": ["正文关键词"],
  "depth": 1,
  "render": "auto"
}
```

响应保留既有字段 `startUrl`、`keywords`、`expandedKeywords`、`depth`、
`render`、`renderMode`、`autoNote`、`siteSearch`、`pagesCrawled`、
`crawledPages`、`pagesFailed`、`totalHits`、`results`、`searchId`，并新增
`discovery` 诊断。`siteSearch` 是已弃用的兼容字段，不再代表独立的固定
CMS 直连流程，新代码应读取 `discovery`。

## 手动冒烟

先启动服务，再使用参数化脚本检查公开网站。关键词应选择当前页面正文中
确实存在、但不依赖标题或 URL 的文本：

```powershell
.\.venv\Scripts\python.exe scripts\smoke_discovery.py `
  "https://www.gamersky.com/" "用户选择的正文关键词"

.\.venv\Scripts\python.exe scripts\smoke_discovery.py `
  "https://www.zycg.gov.cn/" "用户选择的正文关键词"
```

脚本输出 `pagesCrawled`、`totalHits`、`discovery` 和命中页面 URL。
Gamersky 仅用于验证通用规则。公开网站可能随时改版、限流或暂时不可达，
因此手动冒烟不作为持续集成的固定断言，也不表示适配站点永远可用。工具
不会绕过验证码、登录墙或其他访问控制。

## 运行测试

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
```

测试包含发现模型、URL 规范化、解析器、Provider、适配器、预算、正文匹配、
API/任务兼容性、端到端 fixture、冒烟脚本和前端诊断。需要 Chromium 的测试
在浏览器不可用时会跳过。

## 项目结构

```text
server.py          FastAPI 搜索、定位、摘要与问答接口
crawler.py         基础 BFS 与静态/渲染抓取
renderer.py        Playwright 动态入口和分页发现
discovery/         通用发现、适配器、Provider、预算、排序与正文抓取
matcher.py         正文提取、正文专用匹配及旧匹配接口
sitesearch.py      已弃用的旧站内搜索兼容层，内部转发 discovery
jobs.py            定时任务执行与命中去重
scripts/           开发启动与参数化手动冒烟工具
static/            原生单页前端
tests/             pytest 单元、接口、集成与前端测试
docs/superpowers/  设计说明和实施计划
```

## 技术栈与注意事项

FastAPI、httpx、BeautifulSoup、Playwright（Chromium）、原生 HTML/JS，
以及可选的 DeepSeek OpenAI 兼容接口。

- `.env` 和本地任务数据不入库。
- Chromium 二进制安装在用户目录，体积可能较大。
- 自定义动态控件、访问控制、限流或缺失公开入口都可能降低发现覆盖率；
  查看 `discovery` 诊断可区分无结果、来源失败和预算截断。
- AI 扩展词、摘要和问答会消耗模型 token；搜索正文不依赖 AI。
