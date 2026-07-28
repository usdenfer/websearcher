# 站内关键词搜索工具

输入一个网址和若干关键词，自动抓取站内页面并找出关键词出现的所有位置。
支持 JS 动态网站（headless 浏览器渲染 + 自动翻页），并接入 DeepSeek
提供关键词扩展、结果摘要和针对页面内容的智能问答。

## 功能一览

- **站内搜索**：BFS 分层抓取（深度 1~3），9 类命中位置（正文、图片
  alt/src、title/aria 属性、链接文本/地址、meta、表单），高亮片段 +
  跳转定位 + 所在页高亮副本（`/api/locate`）。
- **三种抓取模式**：
  | 模式 | 行为 | 适用 |
  |---|---|---|
  | 自动（默认） | 先静态快搜；零命中或疑似 JS 动态站且命中 ≤3 时自动升级渲染补搜 | 不确定站点类型 |
  | 强制 JS 渲染 | 主页和栏目入口用 headless Chromium 渲染；限量发现的文章正文可经 HTTP 抓取，列表自动翻页（最多 100 页/列表） | 政府站等重度动态站 |
  | 仅静态 | httpx 直接抓取，最快 | 纯静态站 |
- **AI 助手**（需 DeepSeek key）：
  - 搜索前自动扩展相关关键词（失败静默降级）；
  - 「生成 AI 摘要」：基于命中页面流式生成总结；
  - 问答：就本次搜索抓到的页面内容自由提问，SSE 流式回答。
  搜索结果缓存 10 分钟（`cache.py`），摘要/问答基于缓存上下文。
- **旧文找回（多通道）**：针对旧文章沉在栏目列表几十页之后的问题——
  - **站内搜索补充**（`sitesearch.py`）：自动探测政府站 CMS 的搜索接口
    （`searchN.aspx` / `searchClassCount.aspx`），把关键词交给站内搜索、
    抓回结果文章全文补充匹配。能力与官网搜索一致：按标题索引，标题含
    关键词的旧文都能找回；结果页元信息会显示「站内搜索补充 N 页」。
    不支持该接口的站点自动跳过，不影响主流程。
  - **深度翻页**：渲染模式下单列表最多翻 100 页（原 15），每个渲染页
    最多取 60 个子链接（原 25），覆盖动辄上百页的栏目列表。
  - **动态栏目发现补充**：渲染搜索会优先少量抓取动态栏目/列表入口，并从其
    分页中取回文章正文；不受所选深度 1～3 限制，但仍与常规抓取共用 60 页、
    10 分钟预算。
- **定时搜索任务**：页面下方「定时搜索任务」面板创建，时间由用户自选——
  「每天定时 HH:MM」或「每隔 N 小时（1~168）」。后端每 30 秒扫描到点任务，
  抓取与去重逻辑和手动搜索一致（auto 模式同样会自动升级渲染）。
  首次运行建立基线（newHits=null），之后每次运行对比历史命中指纹
  （sha1），有新命中时列表里显示红色「+N 新命中」徽标；支持启停开关、
  立即运行、删除。任务持久化在 `data/jobs.json`（已 gitignore）。

## 快速开始

```bash
# 1. 创建虚拟环境并安装依赖（首次）
python -m venv .venv
.venv\Scripts\python.exe -m pip install playwright fastapi "uvicorn[standard]" httpx pydantic python-dotenv pytest beautifulsoup4
.venv\Scripts\python.exe -m playwright install chromium

# 2. 配置 DeepSeek key（AI 功能必填，不配置则 AI 静默降级）
copy .env.example .env   # 然后编辑 .env 填入 DEEPSEEK_API_KEY

# 3. 启动开发服务（自动使用 .venv，代码改动热重载）
npm run dev              # 默认 http://127.0.0.1:7100
npm run dev -- --port 8080
```

## 使用建议

- 静态站：默认自动模式即可，秒级返回。
- 政府/门户类动态站：自动模式通常能自行识别并补搜；已知是动态站可选
  「强制 JS 渲染」省掉静态那一趟。
- 从首页搜很深的旧内容（如多年前的通知）：选深度 2~3 + 渲染，耗时
  2~8 分钟；已知栏目页地址时直接填栏目页 + 深度 1 更快。
- 渲染模式预算：最多 60 页、单列表最多翻 100 页、整体 10 分钟超时。

## 通用发现与手动冒烟

搜索会综合网站公开的搜索入口、sitemap、Feed、栏目分页和普通 BFS 来发现
候选页，再抓取目标页，以正文可见文本确认关键词。关键词引导的结构化发现
不受 `--depth` 控制的普通 BFS 点击层级限制，但仍受统一时间和页面预算约束。
响应中的 `discovery` 给出实际尝试及成功的来源、候选数量、警告和耗时；
`partial: true` 表示达到预算或部分来源失败时返回的是已完成的部分结果。

启动服务后，可用参数化脚本手动检查公开网站。每次应传入用户选择的正文
关键词，脚本只输出 `pagesCrawled`、`totalHits`、`discovery` 和命中页面 URL：

```powershell
.\.venv\Scripts\python.exe scripts\smoke_discovery.py `
  "https://www.gamersky.com/" "用户选择的正文关键词"

.\.venv\Scripts\python.exe scripts\smoke_discovery.py `
  "https://www.zycg.gov.cn/" "用户选择的正文关键词"
```

Gamersky 仅用于验证通用规则，不配置专用 adapter。线上网站结构和内容会
变化，因此这些检查只手动运行，不作为持续集成的固定断言。中央政府采购网
的 FreeCMS API 失败时，诊断中应能看到 `category` 等降级来源；部分来源
失败也可能同时返回已有结果并标记 `partial`。工具不会绕过验证码、登录墙
或其他访问控制，也不会在脚本中固化真实姓名或业务关键词。

## 运行测试

```bash
.venv\Scripts\python.exe -m pytest tests/ -q
```

测试覆盖单元测试（匹配/缓存/AI 提示词/爬虫）、接口测试（含 mock 的
SSE 流）、真实 Chromium 的渲染测试与前端冒烟测试（搜索→结果渲染→摘要
→问答全链路，AI 调用全部 mock）。无 Chromium 环境时渲染类测试自动跳过。

## 项目结构

```
server.py     FastAPI 入口：/api/search、/api/locate、/api/summarize、/api/ask
crawler.py    BFS 爬虫（静态 + 渲染两种模式）
renderer.py   Playwright headless 渲染、分页识别与翻页抓取
matcher.py    9 类命中匹配、正文提取、JS 动态站嗅探
locator.py    关键词高亮定位代理页
ai.py         DeepSeek 客户端：关键词扩展、摘要/问答提示词、SSE 解析
cache.py      搜索结果缓存（TTL 600s，供摘要/问答复用）
static/       原生前端（单页 index.html）
scripts/dev.mjs  开发启动器（优先使用 .venv，转发 --host/--port）
tests/        pytest 测试与 Playwright 冒烟测试
docs/superpowers/  设计文档与实现计划
```

## 技术栈

FastAPI · httpx · BeautifulSoup · Playwright (Chromium) · 原生 HTML/JS ·
DeepSeek（OpenAI 兼容接口，模型 `deepseek-chat`，可经 `AI_BASE_URL` 替换）

## 注意事项

- `.env` 不入库；Chromium 浏览器二进制安装在用户目录（约 400MB）。
- 渲染模式下若目标页把「栏目菜单」做成分页样式，翻页识别会跳过非分页
  链接并带导航守卫，但极端自定义控件仍可能漏翻。
- AI 扩展词、摘要、问答均消耗 DeepSeek token；无 key 时搜索本体不受影响。
