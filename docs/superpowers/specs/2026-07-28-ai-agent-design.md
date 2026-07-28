# AI Agent 功能（关键词扩展 + 结果摘要 + 站内问答）— 设计文档

日期：2026-07-28
状态：已与用户确认
前置文档：`2026-07-27-site-keyword-search-design.md`（站内关键词搜索工具）

## 1. 目标

在现有关键词搜索工具中接入 AI（DeepSeek，OpenAI 兼容接口）：

- **关键词扩展（自动）**：搜索前 AI 把关键词扩展为最多 5 个相关词一起搜，提高召回
- **结果摘要（按需）**：搜索完成后用户点按钮，AI 阅读命中页面，流式生成总体结论 + 每页一句话摘要
- **站内问答（按需）**：用户基于已抓取页面用自然语言提问，AI 流式回答并附来源链接

## 2. 形态与技术选型

- 后端代理 AI 调用：新增 `ai.py`，用已有 httpx 直连 `https://api.deepseek.com/chat/completions`，不引入 openai SDK；支持流式（SSE）
- API Key 存 `.env`（`DEEPSEEK_API_KEY`），`.gitignore` 拦截；附 `.env.example`；新增唯一依赖 `python-dotenv`
- 配置项（env 可覆盖）：`AI_BASE_URL`（默认 `https://api.deepseek.com`）、`AI_MODEL`（默认 `deepseek-chat`）
- 摘要/问答输出用 SSE 流式推送给前端

## 3. 架构

```
ai.py              DeepSeek 客户端 + 三个提示词组装器（expand/summarize/ask）
cache.py           进程内搜索结果缓存（searchId → 抓取结果 + 页面文本节选）
server.py          搜索自动扩展 + searchId；/api/summarize、/api/ask（SSE）
static/index.html  结果页 AI 区：扩展词展示、摘要卡片（流式）、问答对话（流式）
.env / .env.example
tests/test_ai.py   AI 模块与接口测试（mock AI 响应）
```

## 4. 搜索流程变化

1. 用户点搜索 → 后端先调 AI 扩展：原关键词 + 起始域名发给模型，要求返回 ≤5 个相关词的 JSON 数组
2. 扩展成功 → 用「原关键词 + 扩展词」执行现有抓取匹配；扩展失败/超时（15 秒）→ **静默降级为原关键词**，不影响搜索
3. 搜索完成后，抓取结果（含每个页面的可见文本节选，每页截 3000 字）存入缓存，响应增加：
   - `searchId`：uuid4 hex
   - `expandedKeywords`：AI 追加的扩展词列表（无扩展或降级时为 `[]`）

## 5. 缓存设计（cache.py）

- 进程内 dict：`{searchId: {"ts": float, "result": dict, "pages": [{"url","title","text"}]}}`
- `pages[].text` 为页面可见文本节选（每页截 3000 字）
- TTL 10 分钟；最多 20 条，超出淘汰最旧
- `searchId` 不存在/过期 → 接口返回 404

## 6. SSE 流式协议（/api/summarize、/api/ask 共用）

- `POST /api/summarize` 请求 `{searchId}`；`POST /api/ask` 请求 `{searchId, question}`
- 响应 `text/event-stream`：
  - 内容块：`data: {"type":"delta","text":"..."}`
  - 正常结束：`data: {"type":"done"}`
  - 出错：`data: {"type":"error","message":"..."}`（包括未配置 Key、AI 超时、上游错误）
- 前端用 fetch + ReadableStream 手动解析 SSE（EventSource 不支持 POST），边收边渲染

## 7. AI 上下文策略（成本控制）

- **扩展**：输入关键词 + 起始域名；输出 ≤5 个相关词（JSON 数组）；解析失败（非 JSON）→ 降级为空扩展。每次约几百 token
- **摘要**：每命中页发送标题 + 最多 3 条命中片段（各截 200 字），总输入限约 4000 字；输出总体结论 2~3 句 + 每页一句话（按页分组）
- **问答**：发送问题 + 命中页面正文节选（围绕关键词的上下文窗口，总量限约 6000 字）；要求基于内容回答、附来源 URL；内容不足时明说
- AI 调用超时：扩展 15 秒；摘要/问答 90 秒

## 8. 前端 AI 区（结果页顶部）

- 扩展词行：`AI 扩展：+省长、+予波同志`（无扩展或降级时不显示）
- 「生成 AI 摘要」按钮 → 摘要卡片流式打字渲染；文本中的 URL 自动转为可点击链接
- 问答输入框 + 提问按钮 → 问答对逐条追加（问在上答在下），答案流式显示、来源链接可点
- 流式期间对应按钮禁用；AI 出错显示明确错误条，不影响已展示的搜索结果

## 9. 异常处理

- 未配置 `DEEPSEEK_API_KEY` → 扩展静默跳过；摘要/问答推送 error 事件「未配置 API Key」
- searchId 不存在/过期 → 404 JSON
- DeepSeek 返回错误状态/超时 → error 事件带简要原因
- AI 任何失败都不影响基础关键词搜索功能

## 10. 测试

- `ai.py`：mock httpx 响应——扩展 JSON 解析、坏 JSON/非数组降级、SSE 流式块解析
- 接口（monkeypatch AI 函数）：
  - 搜索后响应含 `searchId` 与正确 `expandedKeywords`；AI 扩展失败时降级且搜索正常
  - `/api/summarize`、`/api/ask` 的 SSE 事件序列（delta…→done）
  - 未知 searchId → 404；未配置 Key → error 事件
  - 缓存 TTL/容量淘汰
- 前端：手动验证扩展词展示、摘要/问答流式渲染、错误条
