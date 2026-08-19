# 单源端到端 RAG 知识库（云南政府采购网）设计

日期：2026-08-18
状态：已与用户确认
前置：`2026-07-28-ai-agent-design.md`、`2026-07-28-generalized-site-discovery-design.md`

## 1. 目标

新建独立项目 `ai-procurement-intelligence`，以**云南政府采购网（yngp）**为
首个数据源，把公开采购公告「扒下来 → 清洗 → 切块 → 向量化 → 入库」，并
提供基于检索增强生成（RAG）的语义问答能力。本设计只覆盖**单源端到端**
最小闭环，用于验证整条链路质量；全国多源、调度、后台等留待后续子项目。

## 2. 背景与问题

现有 `web_keyword_catcher` 是「即搜即弃」的关键词搜索工具，正文不落盘，
无法沉淀为可检索的知识库。用户需要把公告正文长期保存并支持语义问答。

`web_keyword_catcher` 已具备可复用的抓取与 AI 能力：

- `discovery/`：通用发现引擎 + `YngpAdapter`/`YunnanCmsAdapter` 适配器
- `crawler.py`：BFS 抓取、重试、预算、并发
- `renderer.py`：Playwright 渲染动态页
- `ai.py`：DeepSeek 客户端（chat / stream / expand / summarize / ask）

本设计采用**轻量复用**：新项目直接依赖现有仓库的上述模块，不重构。

## 3. 范围

### 3.1 范围内

- 抓取云南政府采购网公开采购公告（正文页）。
- HTML → 正文纯文本（readability-lxml）。
- 按公告切块，附标题、发布日期、URL 元数据。
- 云端 OpenAI 兼容 embedding API 向量化。
- Chroma 向量库持久化存储。
- RAG 问答：问题 → 向量检索 top-k → DeepSeek 生成 → 答案 + 来源 URL。
- 原始正文 + 元数据落盘（SQLite）。

### 3.2 范围外

- 全国多源站点、多源调度与增量监控（后续子项目）。
- Crawl4AI、Prefect、PostgreSQL/pgvector、Vue 后台。
- 用户权限、多用户、Web 管理界面（先以脚本 / 最小 API 验证）。
- 绕过验证码、登录墙。

## 4. 总体架构

```
                    ai-procurement-intelligence
                              │
            ┌─────────────────┼──────────────────┐
            ▼                 ▼                  ▼
      采集(crawl)         内容处理(clean)      向量化(embed)
   复用 discovery+crawler   readability-lxml   云端 embedding API
            │                 │                  │
            └────────┬────────┘                  │
                     ▼                           │
             切块(chunk) ────────────────────────┘
                     │
                     ▼
            Chroma 向量库 (持久化)
                     ▲
                     │  top-k 检索
                     ▼
            RAG 问答 (DeepSeek chat)
                     │
                     ▼
            答案 + 来源 URL
```

数据流（离线入库）：

```
discovery/crawler 抓 yngp 公告 → CrawledPage(html, url)
  → readability → 正文文本
  → 切块（一篇公告一个 chunk，超长按段落切）
  → embedding API → 向量
  → Chroma.upsert（向量 + 元数据：标题/日期/URL）
  → 正文 + 元数据写入 SQLite
```

数据流（在线问答）：

```
问题 → embedding → Chroma 检索 top-k → 拼接上下文
  → DeepSeek chat 生成 → 答案 + 引用来源 URL
```

## 5. 复用方式（轻量，不改现有仓库）

新项目与 `web_keyword_catcher` 同级 clone，入口脚本将后者根目录加入
`sys.path`（或采用 git submodule 锁定版本）：

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "web_keyword_catcher"))

from discovery import discover_pages  # noqa: E402
from crawler import crawl             # noqa: E402
from ai import chat                   # noqa: E402
```

不修改 `web_keyword_catcher` 任何文件。

## 6. 组件

### 6.1 采集 `ingest.py`

- 调用现有 `discovery`/`crawler` 抓取 yngp 公告，产出
  `(url, html, title, published_date)` 列表。
- 复用现有预算 / 限流 / 重试；首期用「全量」思路（放宽页数上限），
  具体抓取深度由配置文件控制。

### 6.2 内容处理 `clean.py`

- `readability.Document(html).summary()` 取主内容，再转纯文本；该实现
  **自含于新项目**，不依赖 `web_keyword_catcher` 的 `matcher` 模块。
- 正文为空或 readability 抛异常时回退到「整页可见文本」（bs4 去脚本/样式）。
- 产出 `(url, clean_text, title, published_date)`。

### 6.3 切块 `chunk.py`

- 一篇公告一个 chunk；正文超过阈值（默认 1500 字）时按段落/标题二次切分。
- 每个 chunk 附元数据：`{url, title, published_date, chunk_index}`。

### 6.4 向量化 `embed.py`

- OpenAI 兼容 `POST {base_url}/embeddings`，返回向量。
- 配置（env）：`EMBEDDING_BASE_URL`（默认取 `AI_BASE_URL`）、
  `EMBEDDING_MODEL`（默认 `bge-large-zh`）、`EMBEDDING_API_KEY`
  （默认取 `DEEPSEEK_API_KEY`）。
- 封装为 `embed(texts: list[str]) -> list[list[float]]`，供入库与查询共用。

### 6.5 存储 `store.py`

- Chroma `PersistentClient`，本地目录 `data/chroma/`；collection 名
  `yngp_procurements`。
- SQLite（标准库 `sqlite3`）存原始正文 + 元数据，表：

```sql
CREATE TABLE documents (
    id TEXT PRIMARY KEY,        -- url 的 sha1
    url TEXT NOT NULL,
    title TEXT,
    published_date TEXT,
    clean_text TEXT NOT NULL,
    ingested_at TEXT NOT NULL
);
```

- 幂等：按 `id`（URL 哈希）upsert，重复抓取不重复入库。

### 6.6 问答 `ask.py`

- 查询向量化 → Chroma 检索 top-k（默认 5）→ 拼装上下文（附来源 URL）
  → `ai.chat()` 生成 → 返回答案 + 引用。
- 复用 `ai.py` 的 `chat` 与 `AIError` 处理。

### 6.7 入口 `main.py`

- 子命令：`ingest`（抓取入库）、`ask "问题"`（问答）、`serve`（可选最小
  FastAPI 端点，后续再做）。

## 7. 依赖

新项目 `requirements.txt`：

```text
readability-lxml
chromadb
httpx
python-dotenv
beautifulsoup4
```

（`discovery`/`crawler`/`ai` 等复用模块的依赖由 `web_keyword_catcher` 环境
提供；新项目自身仅声明 RAG 层新增依赖。）

## 8. 错误处理

- 单篇公告抓取 / 清洗失败：跳过并记录，不阻断整体入库。
- embedding API 失败 / 超时：记录并跳过该批，不丢失已入库数据。
- 检索无结果：明确返回「知识库中未找到相关内容」。
- 任何失败不影响 `web_keyword_catcher` 原有功能。

## 9. 测试

- `clean`：readability 提取正文、导航噪声去除、空 HTML 回退。
- `chunk`：单公告单 chunk、超长公告切分、元数据完整性。
- `embed`：请求 payload 字段、失败降级（mock httpx）。
- `store`：幂等 upsert、SQLite 写入、Chroma 增删查。
- `ask`：检索结果为空、有结果时 prompt 结构、来源 URL 注入（mock chat）。
- 复用模块的既有测试（`web_keyword_catcher/tests/`）保持通过。

测试不依赖真实站点与真实 embedding API，全部用本地 fixture + mock。

## 10. 完成标准

- 云南政府采购网一批公告成功入库（正文 + 向量 + 元数据）。
- 对知识库内已有内容提问，能返回语义相关答案并附可点击来源 URL。
- 重复执行 `ingest` 不产生重复记录（幂等）。
- 新增测试全绿；`web_keyword_catcher` 复用模块测试不受影响。
