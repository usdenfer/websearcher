# 单源端到端 RAG（云南政府采购网）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新建 `ai-procurement-intelligence` 项目，以云南政府采购网为首个数据源，跑通「抓取 → 清洗 → 切块 → 向量化 → 入库 → RAG 问答」最小闭环。

**Architecture:** 轻量复用 `web_keyword_catcher` 的 `discovery`/`crawler`/`ai` 模块（同级 clone + `sys.path`，零重构）。新项目自含 `clean`/`chunk`/`embed`/`store`/`ingest`/`ask`/`main` 七个模块，用 SQLite 存原始正文、Chroma 存向量。

**Tech Stack:** Python 3.12, pytest 9, httpx, readability-lxml, chromadb, sqlite3（标准库）, DeepSeek/OpenAI 兼容 chat 与 embedding。

---

## 前置约定（全计划通用）

- 新项目目录：与 `web_keyword_catcher` 同级，即 `..\ai-procurement-intelligence`（本计划记为 `$ROOT`）。
- 先完成「克隆复用仓库」：

```powershell
cd D:\WebProjects
git clone D:\WebProjects\web_keyword_catcher web_keyword_catcher  # 若已存在则跳过
```

- 新项目所有运行与测试都在其独立 venv 中：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt
```

- 复用模块的导入统一走 `_bootstrap.ensure_wkc()`（见 Task 1），不得在业务模块里手写 `sys.path`。

---

### Task 1: 项目脚手架与配置

**Files:**
- Create: `$ROOT/requirements.txt`
- Create: `$ROOT/requirements-dev.txt`
- Create: `$ROOT/_bootstrap.py`
- Create: `$ROOT/config.py`
- Create: `$ROOT/tests/__init__.py`
- Create: `$ROOT/tests/conftest.py`
- Test: `$ROOT/tests/test_config.py`

- [ ] **Step 1: 写 requirements**

`requirements.txt`：

```text
readability-lxml
chromadb
httpx
python-dotenv
beautifulsoup4
playwright
```

`requirements-dev.txt`：

```text
pytest
```

- [ ] **Step 2: 写 `_bootstrap.py`**

```python
"""Insert the sibling web_keyword_catcher repo onto sys.path for reuse."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _wkc_path() -> Path:
    override = os.environ.get("WKC_PATH")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "web_keyword_catcher"


def ensure_wkc() -> Path:
    path = _wkc_path()
    if not path.exists():
        raise FileNotFoundError(
            f"web_keyword_catcher not found at {path}; "
            "clone it alongside or set WKC_PATH"
        )
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
    return path
```

- [ ] **Step 3: 写 `config.py`**

```python
"""Runtime configuration via environment variables."""
from __future__ import annotations

import os


def embedding_base_url() -> str:
    return os.environ.get(
        "EMBEDDING_BASE_URL",
        os.environ.get("AI_BASE_URL", "https://api.vectorengine.cn/v1"),
    )


def embedding_model() -> str:
    return os.environ.get("EMBEDDING_MODEL", "bge-large-zh")


def embedding_api_key() -> str:
    return os.environ.get("EMBEDDING_API_KEY", os.environ.get("DEEPSEEK_API_KEY", ""))


def data_dir() -> str:
    return os.environ.get("AI_PROC_DATA_DIR", "data")
```

- [ ] **Step 4: 写 `tests/conftest.py` 与 `tests/__init__.py`**

`tests/__init__.py` 为空文件。

`tests/conftest.py`：

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
```

- [ ] **Step 5: 写配置测试（先失败）**

`tests/test_config.py`：

```python
import config


def test_embedding_defaults(monkeypatch):
    monkeypatch.delenv("EMBEDDING_BASE_URL", raising=False)
    monkeypatch.delenv("AI_BASE_URL", raising=False)
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
    monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert config.embedding_base_url() == "https://api.vectorengine.cn/v1"
    assert config.embedding_model() == "bge-large-zh"
    assert config.embedding_api_key() == ""


def test_embedding_env_overrides(monkeypatch):
    monkeypatch.setenv("EMBEDDING_BASE_URL", "https://x.test/v1")
    monkeypatch.setenv("EMBEDDING_MODEL", "m3")
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-abc")
    assert config.embedding_base_url() == "https://x.test/v1"
    assert config.embedding_model() == "m3"
    assert config.embedding_api_key() == "sk-abc"


def test_embedding_api_key_falls_back_to_deepseek(monkeypatch):
    monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds")
    assert config.embedding_api_key() == "sk-ds"
```

- [ ] **Step 6: 运行测试验证失败**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_config.py -q`
Expected: FAIL，`ModuleNotFoundError: config`（模块尚未创建时）或「函数未定义」。

- [ ] **Step 7: 创建 `config.py`（同 Step 3 内容）后验证通过**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_config.py -q`
Expected: PASS（3 passed）。

- [ ] **Step 8: 提交**

```powershell
git init
git add requirements.txt requirements-dev.txt _bootstrap.py config.py tests/
git commit -m "chore: scaffold project with bootstrap and config"
```

---

### Task 2: 正文清洗 `clean.py`

**Files:**
- Create: `$ROOT/clean.py`
- Test: `$ROOT/tests/test_clean.py`

- [ ] **Step 1: 写失败测试**

`tests/test_clean.py`：

```python
from bs4 import BeautifulSoup

import clean


class FakeDoc:
    def __init__(self, summary):
        self._summary = summary

    def summary(self):
        return self._summary


def test_clean_text_uses_readability_when_available(monkeypatch):
    monkeypatch.setattr(
        clean, "Document",
        lambda html: FakeDoc("<html><body><article>正文内容</article></body></html>"),
    )
    assert "正文内容" in clean.clean_text("<html>raw</html>")


def test_clean_text_falls_back_to_visible_text_when_readability_missing(monkeypatch):
    monkeypatch.setattr(clean, "Document", None)
    html = "<html><body><script>var x</script><p>可见文本</p></body></html>"
    assert clean.clean_text(html) == "可见文本"


def test_clean_text_falls_back_when_summary_empty(monkeypatch):
    monkeypatch.setattr(clean, "Document", lambda html: FakeDoc(""))
    html = "<html><body><p>兜底正文</p></body></html>"
    assert clean.clean_text(html) == "兜底正文"


def test_clean_text_falls_back_when_document_raises(monkeypatch):
    def boom(html):
        raise RuntimeError("parse failed")

    monkeypatch.setattr(clean, "Document", boom)
    html = "<html><body><p>异常兜底</p></body></html>"
    assert clean.clean_text(html) == "异常兜底"
```

- [ ] **Step 2: 运行验证失败**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_clean.py -q`
Expected: FAIL（`clean` 模块不存在）。

- [ ] **Step 3: 实现 `clean.py`**

```python
"""HTML main-content extraction for the RAG ingest pipeline."""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

try:
    from readability import Document
except ImportError:
    Document = None


def _node_text(node) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


def _visible_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return _node_text(soup)


def clean_text(html: str) -> str:
    """Return clean main-content text; fall back to whole-page visible text."""
    if Document is not None:
        try:
            summary = Document(html).summary()
        except Exception:
            summary = ""
        if summary:
            text = _node_text(BeautifulSoup(summary, "html.parser"))
            if text:
                return text
    return _visible_text(html)
```

- [ ] **Step 4: 运行验证通过**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_clean.py -q`
Expected: PASS（4 passed）。

- [ ] **Step 5: 提交**

```powershell
git add clean.py tests/test_clean.py
git commit -m "feat: readability-based content cleaning with fallback"
```

---

### Task 3: 切块 `chunk.py`

**Files:**
- Create: `$ROOT/chunk.py`
- Test: `$ROOT/tests/test_chunk.py`

- [ ] **Step 1: 写失败测试**

`tests/test_chunk.py`：

```python
import chunk


def test_short_document_is_single_chunk():
    chunks = chunk.chunk_document(
        "https://x.test/a", "一段不长的公告正文", "标题A", "2026-08-01"
    )
    assert len(chunks) == 1
    c = chunks[0]
    assert c.text == "一段不长的公告正文"
    assert c.url == "https://x.test/a"
    assert c.title == "标题A"
    assert c.published_date == "2026-08-01"
    assert c.index == 0
    assert c.metadata()["chunk_index"] == 0


def test_long_document_splits_by_paragraph():
    para = "这是很长的一段正文内容。" * 100
    text = "\n\n".join([para, para, para])
    chunks = chunk.chunk_document("https://x.test/b", text)
    assert len(chunks) > 1
    assert [c.index for c in chunks] == list(range(len(chunks)))
    assert all(c.text.strip() for c in chunks)


def test_blank_document_produces_no_chunks():
    assert chunk.chunk_document("https://x.test/c", "   \n  ") == []
```

- [ ] **Step 2: 运行验证失败**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_chunk.py -q`
Expected: FAIL（`chunk` 模块不存在）。

- [ ] **Step 3: 实现 `chunk.py`**

```python
"""Split cleaned documents into retrievable chunks with metadata."""
from __future__ import annotations

import re
from dataclasses import dataclass

MAX_CHUNK_CHARS = 1500


@dataclass
class Chunk:
    text: str
    url: str
    title: str
    published_date: str
    index: int

    def metadata(self) -> dict:
        return {
            "url": self.url,
            "title": self.title,
            "published_date": self.published_date,
            "chunk_index": self.index,
        }


def _split_long_text(text: str, limit: int = MAX_CHUNK_CHARS) -> list[str]:
    if len(text) <= limit:
        return [text]
    paragraphs = re.split(r"\n+", text)
    parts: list[str] = []
    current = ""
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if current and len(current) + len(para) + 1 > limit:
            parts.append(current)
            current = para
        else:
            current = f"{current}\n{para}" if current else para
    if current:
        parts.append(current)
    return parts


def chunk_document(
    url: str,
    clean_text: str,
    title: str = "",
    published_date: str = "",
) -> list[Chunk]:
    parts = _split_long_text(clean_text)
    return [
        Chunk(text=part, url=url, title=title,
              published_date=published_date, index=i)
        for i, part in enumerate(parts)
        if part.strip()
    ]
```

- [ ] **Step 4: 运行验证通过**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_chunk.py -q`
Expected: PASS（3 passed）。

- [ ] **Step 5: 提交**

```powershell
git add chunk.py tests/test_chunk.py
git commit -m "feat: per-notice chunking with metadata"
```

---

### Task 4: 向量化 `embed.py`

**Files:**
- Create: `$ROOT/embed.py`
- Test: `$ROOT/tests/test_embed.py`

- [ ] **Step 1: 写失败测试**

`tests/test_embed.py`：

```python
import asyncio

import httpx
import pytest

import embed
from embed import EmbedError


def _run(coro):
    return asyncio.run(coro)


def _client_with(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_embed_returns_vectors_and_sends_payload(monkeypatch):
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-test")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "https://x.test/v1")
    monkeypatch.setenv("EMBEDDING_MODEL", "m3")
    captured = {}

    def handler(request):
        captured["json"] = request.json()
        captured["auth"] = request.headers["authorization"]
        return httpx.Response(
            200, json={"data": [{"embedding": [0.1, 0.2]},
                                {"embedding": [0.3, 0.4]}]}, request=request)

    async def run():
        async with _client_with(handler) as client:
            monkeypatch.setattr(embed.httpx, "AsyncClient", lambda *a, **k: client)
            return await embed.embed(["a", "b"])

    vectors = _run(run())
    assert vectors == [[0.1, 0.2], [0.3, 0.4]]
    assert captured["json"] == {"model": "m3", "input": ["a", "b"]}
    assert captured["auth"] == "Bearer sk-test"


def test_embed_empty_input_returns_empty():
    assert _run(embed.embed([])) == []


def test_embed_raises_without_api_key(monkeypatch):
    monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(EmbedError):
        _run(embed.embed(["a"]))


def test_embed_raises_on_http_error(monkeypatch):
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-test")

    def handler(request):
        return httpx.Response(401, json={}, request=request)

    async def run():
        async with _client_with(handler) as client:
            monkeypatch.setattr(embed.httpx, "AsyncClient", lambda *a, **k: client)
            return await embed.embed(["a"])

    with pytest.raises(EmbedError):
        _run(run())


def test_embed_raises_on_malformed_response(monkeypatch):
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-test")

    def handler(request):
        return httpx.Response(200, json={"data": "bad"}, request=request)

    async def run():
        async with _client_with(handler) as client:
            monkeypatch.setattr(embed.httpx, "AsyncClient", lambda *a, **k: client)
            return await embed.embed(["a"])

    with pytest.raises(EmbedError):
        _run(run())
```

- [ ] **Step 2: 运行验证失败**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_embed.py -q`
Expected: FAIL（`embed` 模块不存在）。

- [ ] **Step 3: 实现 `embed.py`**

```python
"""OpenAI-compatible embedding client."""
from __future__ import annotations

import httpx

from config import embedding_api_key, embedding_base_url, embedding_model


class EmbedError(Exception):
    pass


async def embed(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    if not embedding_api_key():
        raise EmbedError("未配置 embedding API key")
    payload = {"model": embedding_model(), "input": texts}
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            resp = await client.post(
                f"{embedding_base_url()}/embeddings",
                json=payload,
                headers={"Authorization": f"Bearer {embedding_api_key()}"},
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise EmbedError(f"embedding 请求失败: {exc}") from exc
    try:
        data = resp.json()["data"]
        return [item["embedding"] for item in data]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise EmbedError("embedding 返回格式异常") from exc
```

- [ ] **Step 4: 运行验证通过**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_embed.py -q`
Expected: PASS（5 passed）。

- [ ] **Step 5: 提交**

```powershell
git add embed.py tests/test_embed.py
git commit -m "feat: OpenAI-compatible embedding client"
```

---

### Task 5: 存储 `store.py`

**Files:**
- Create: `$ROOT/store.py`
- Test: `$ROOT/tests/test_store.py`

- [ ] **Step 1: 写失败测试**

`tests/test_store.py`：

```python
import chromadb

import store
from chunk import Chunk


def make_store(tmp_path):
    client = chromadb.Client()  # in-memory, no onnx model download
    return store.Store(root=str(tmp_path), chroma_client=client)


def test_save_document_is_idempotent(tmp_path):
    s = make_store(tmp_path)
    doc_id = s.save_document("https://x.test/a", "正文", "标题", "2026-08-01")
    assert doc_id == s.save_document("https://x.test/a", "新正文", "新标题", "2026-08-02")
    row = s.conn.execute(
        "SELECT clean_text, title FROM documents WHERE id=?", (doc_id,)
    ).fetchone()
    assert row == ("新正文", "新标题")


def test_upsert_chunks_and_query(tmp_path):
    s = make_store(tmp_path)
    doc_id = s.save_document("https://x.test/a", "正文", "标题", "2026-08-01")
    chunks = [Chunk(text="第一块", url="https://x.test/a",
                    title="标题", published_date="2026-08-01", index=0)]
    s.upsert_chunks(doc_id, chunks, [[0.1, 0.2]])
    results = s.query([0.1, 0.2], top_k=1)
    assert len(results) == 1
    assert results[0]["text"] == "第一块"
    assert results[0]["url"] == "https://x.test/a"


def test_upsert_chunks_no_chunks_is_noop(tmp_path):
    s = make_store(tmp_path)
    s.upsert_chunks("doc", [], [])
    assert s.query([0.0, 0.0], top_k=1) == []
```

- [ ] **Step 2: 运行验证失败**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_store.py -q`
Expected: FAIL（`store` 模块不存在）。

- [ ] **Step 3: 实现 `store.py`**

```python
"""Persistence: SQLite for raw documents, Chroma for vectors."""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import chromadb

from chunk import Chunk
from config import data_dir


def _doc_id(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()


class Store:
    def __init__(self, root: str | None = None, chroma_client=None):
        root = root or data_dir()
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.root / "documents.db")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS documents ("
            " id TEXT PRIMARY KEY, url TEXT NOT NULL, title TEXT,"
            " published_date TEXT, clean_text TEXT NOT NULL,"
            " ingested_at TEXT NOT NULL)"
        )
        self.chroma = chroma_client or chromadb.PersistentClient(
            path=str(self.root / "chroma")
        )
        self.collection = self.chroma.get_or_create_collection(
            "yngp_procurements", embedding_function=None
        )

    def save_document(
        self, url: str, clean_text: str, title: str, published_date: str
    ) -> str:
        doc_id = _doc_id(url)
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT INTO documents"
            "(id, url, title, published_date, clean_text, ingested_at)"
            " VALUES(?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET"
            " clean_text=excluded.clean_text,"
            " title=excluded.title,"
            " published_date=excluded.published_date,"
            " ingested_at=excluded.ingested_at",
            (doc_id, url, title, published_date, clean_text, now),
        )
        self.conn.commit()
        return doc_id

    def upsert_chunks(
        self, doc_id: str, chunks: list[Chunk], vectors: list[list[float]]
    ) -> None:
        if not chunks:
            return
        self.collection.upsert(
            ids=[f"{doc_id}:{c.index}" for c in chunks],
            embeddings=vectors,
            documents=[c.text for c in chunks],
            metadatas=[c.metadata() for c in chunks],
        )

    def query(self, vector: list[float], top_k: int) -> list[dict]:
        result = self.collection.query(
            query_embeddings=[vector],
            n_results=top_k,
            include=["documents", "metadatas", "distances"],
        )
        docs = result.get("documents") or [[]]
        metas = result.get("metadatas") or [[]]
        out: list[dict] = []
        for doc, meta in zip(docs[0], metas[0]):
            out.append({"text": doc, "url": (meta or {}).get("url", "")})
        return out
```

- [ ] **Step 4: 运行验证通过**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_store.py -q`
Expected: PASS（3 passed）。

- [ ] **Step 5: 提交**

```powershell
git add store.py tests/test_store.py
git commit -m "feat: SQLite document store and Chroma vector store"
```

---

### Task 6: 采集源 `source.py`

**Files:**
- Create: `$ROOT/source.py`
- Test: `$ROOT/tests/test_source.py`

- [ ] **Step 1: 写失败测试**

`tests/test_source.py`：

```python
import source


def test_extract_page_fields_from_crawled_page():
    fake_page = type("P", (), {
        "url": "https://x.test/a",
        "html": "<html><title>T</title><body>x</body></html>",
    })()
    title = source._page_title(fake_page)
    assert title == "T"


def test_extract_page_fields_title_missing():
    fake_page = type("P", (), {
        "url": "https://x.test/a",
        "html": "<html><body>x</body></html>",
    })()
    assert source._page_title(fake_page) == ""
```

- [ ] **Step 2: 运行验证失败**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_source.py -q`
Expected: FAIL（`source` 模块不存在）。

- [ ] **Step 3: 实现 `source.py`**

```python
"""Yield crawled procurement pages from the reused web_keyword_catcher crawler."""
from __future__ import annotations

from collections.abc import AsyncIterator

from bs4 import BeautifulSoup

YNGP_START = "http://www.yngp.com/"


def _page_title(page) -> str:
    soup = BeautifulSoup(page.html, "html.parser")
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    return ""


async def yngp_pages() -> AsyncIterator[tuple[str, str, str, str]]:
    """Yield (url, html, title, published_date) for yngp procurement notices.

    Thin adapter over the reused crawler. The reused `crawler.crawl` requires
    keywords; here we drive a discovery-first crawl with an empty keyword so
    the site's own index and category pagination surface the notices.
    """
    import _bootstrap
    _bootstrap.ensure_wkc()

    from crawler import crawl  # noqa: E402

    result = await crawl(YNGP_START, [], depth=1, render="archive")
    for page in result.pages:
        yield page.url, page.html, _page_title(page), ""
```

- [ ] **Step 4: 运行验证通过**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_source.py -q`
Expected: PASS（2 passed）。

- [ ] **Step 5: 提交**

```powershell
git add source.py tests/test_source.py
git commit -m "feat: yngp crawl source adapter"
```

> 说明：`source.yngp_pages` 里的 `crawl(...)` 调用为对接真实爬虫的薄适配，属集成冒烟范围，本计划不为其写单元测试（测试用假 source，见 Task 7）。若实际 `crawl` 签名与本处不符，以 `web_keyword_catcher` 当前 `crawler.py` 为准调整参数，并保留 `yngp_pages` 的产出契约 `(url, html, title, published_date)` 不变。

---

### Task 7: 入库编排 `ingest.py`

**Files:**
- Create: `$ROOT/ingest.py`
- Test: `$ROOT/tests/test_ingest.py`

- [ ] **Step 1: 写失败测试**

`tests/test_ingest.py`：

```python
import asyncio

import chromadb

import ingest
import store
from store import Store


class FakeSource:
    def __init__(self, pages):
        self._pages = pages

    async def __aiter__(self):
        for p in self._pages:
            yield p


async def _embed_fake(texts):
    return [[0.0] * 3 for _ in texts]


def make_store(tmp_path):
    return Store(root=str(tmp_path), chroma_client=chromadb.Client())


def test_ingest_persists_documents_and_chunks(tmp_path, monkeypatch):
    pages = [
        ("https://x.test/a", "<html><p>公告正文A</p></html>", "标题A", "2026-08-01"),
        ("https://x.test/b", "<html><p>公告正文B</p></html>", "标题B", "2026-08-02"),
    ]
    s = make_store(tmp_path)
    counts = asyncio.run(ingest.ingest(FakeSource(pages), s, _embed_fake))
    assert counts == {"documents": 2, "chunks": 2}
    row = s.conn.execute(
        "SELECT COUNT(*) FROM documents"
    ).fetchone()
    assert row[0] == 2
    assert s.collection.count() == 2


def test_ingest_skips_failed_clean_but_keeps_others(tmp_path, monkeypatch):
    def broken_clean(html):
        raise RuntimeError("clean failed")

    monkeypatch.setattr(ingest, "clean_text", broken_clean)
    pages = [("https://x.test/a", "<html>x</html>", "", "")]
    s = make_store(tmp_path)
    counts = asyncio.run(ingest.ingest(FakeSource(pages), s, _embed_fake))
    assert counts == {"documents": 0, "chunks": 0, "errors": 1}


def test_ingest_skips_failed_embed_but_keeps_others(tmp_path, monkeypatch):
    async def broken_embed(texts):
        raise RuntimeError("embed failed")

    pages = [("https://x.test/a", "<html><p>x</p></html>", "", "")]
    s = make_store(tmp_path)
    counts = asyncio.run(ingest.ingest(FakeSource(pages), s, broken_embed))
    assert counts == {"documents": 0, "chunks": 0, "errors": 1}
```

- [ ] **Step 2: 运行验证失败**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_ingest.py -q`
Expected: FAIL（`ingest` 模块不存在）。

- [ ] **Step 3: 实现 `ingest.py`**

```python
"""Orchestrate source -> clean -> chunk -> embed -> store."""
from __future__ import annotations

from clean import clean_text
from chunk import chunk_document
from embed import embed
from store import Store


async def ingest(source, store: Store, embed_fn=embed) -> dict:
    """Consume (url, html, title, date) pages into the store.

    Returns counts: {"documents": int, "chunks": int, "errors": int}.
    """
    counts = {"documents": 0, "chunks": 0, "errors": 0}
    async for url, html, title, date in source:
        try:
            text = clean_text(html)
            chunks = chunk_document(url, text, title, date)
            if not chunks:
                continue
            vectors = await embed_fn([c.text for c in chunks])
            doc_id = store.save_document(url, text, title, date)
            store.upsert_chunks(doc_id, chunks, vectors)
            counts["documents"] += 1
            counts["chunks"] += len(chunks)
        except Exception:
            counts["errors"] += 1
    return counts
```

- [ ] **Step 4: 运行验证通过**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_ingest.py -q`
Expected: PASS（3 passed）。

- [ ] **Step 5: 提交**

```powershell
git add ingest.py tests/test_ingest.py
git commit -m "feat: ingest orchestration with per-item failure isolation"
```

---

### Task 8: RAG 问答 `ask.py`

**Files:**
- Create: `$ROOT/ask.py`
- Test: `$ROOT/tests/test_ask.py`

- [ ] **Step 1: 写失败测试**

`tests/test_ask.py`：

```python
import asyncio

import chromadb

import ask
import store
from store import Store
from chunk import Chunk


def make_store(tmp_path):
    return Store(root=str(tmp_path), chroma_client=chromadb.Client())


def seed(store):
    doc_id = store.save_document(
        "https://x.test/a", "某学校设备采购项目，预算 120 万元", "设备采购", "2026-08-01")
    store.upsert_chunks(doc_id, [
        Chunk(text="某学校设备采购项目，预算 120 万元", url="https://x.test/a",
              title="设备采购", published_date="2026-08-01", index=0)],
        [[1.0, 0.0]])


def test_ask_builds_answer_with_sources(tmp_path, monkeypatch):
    s = make_store(tmp_path)
    seed(s)

    captured = {}

    async def fake_embed(texts):
        return [[1.0, 0.0]]

    async def fake_chat(messages, max_tokens=4000, timeout=180.0):
        captured["messages"] = messages
        return "答案是预算 120 万元。"

    monkeypatch.setattr(ask, "embed", fake_embed)
    monkeypatch.setattr(ask, "chat", fake_chat)

    result = asyncio.run(ask.ask("预算多少？", s))
    assert result["answer"] == "答案是预算 120 万元。"
    assert "https://x.test/a" in result["sources"]
    user_content = captured["messages"][-1]["content"]
    assert "预算多少？" in user_content
    assert "https://x.test/a" in user_content


def test_ask_returns_empty_when_no_results(tmp_path, monkeypatch):
    s = make_store(tmp_path)

    async def fake_embed(texts):
        return [[1.0, 0.0]]

    monkeypatch.setattr(ask, "embed", fake_embed)
    result = asyncio.run(ask.ask("预算多少？", s))
    assert result["answer"] == "知识库中未找到相关内容。"
    assert result["sources"] == []
```

- [ ] **Step 2: 运行验证失败**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_ask.py -q`
Expected: FAIL（`ask` 模块不存在）。

- [ ] **Step 3: 实现 `ask.py`**

```python
"""Retrieve relevant chunks and answer with the reused DeepSeek client."""
from __future__ import annotations

import _bootstrap
_bootstrap.ensure_wkc()

from ai import chat  # noqa: E402

from embed import embed  # noqa: E402

_SYSTEM = (
    "你是采购公告问答助手。只根据给定的公告片段回答，"
    "引用来源时附上片段对应的完整 URL。内容不足时明确说明，不要编造。"
)


def _build_user(question: str, hits: list[dict]) -> str:
    blocks = []
    for hit in hits:
        blocks.append(f"来源：{hit['url']}\n内容：{hit['text']}")
    context = "\n\n".join(blocks) if blocks else "（无相关公告片段）"
    return f"公告片段：\n{context}\n\n问题：{question}"


async def ask(question: str, store, top_k: int = 5) -> dict:
    vectors = await embed([question])
    hits = store.query(vectors[0], top_k=top_k)
    if not hits:
        return {"answer": "知识库中未找到相关内容。", "sources": []}
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": _build_user(question, hits)},
    ]
    answer = await chat(messages)
    sources = list(dict.fromkeys(h["url"] for h in hits if h["url"]))
    return {"answer": answer, "sources": sources}
```

- [ ] **Step 4: 运行验证通过**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_ask.py -q`
Expected: PASS（2 passed）。

- [ ] **Step 5: 提交**

```powershell
git add ask.py tests/test_ask.py
git commit -m "feat: RAG question answering with source citations"
```

---

### Task 9: CLI 入口 `main.py`

**Files:**
- Create: `$ROOT/main.py`
- Test: `$ROOT/tests/test_main.py`

- [ ] **Step 1: 写失败测试**

`tests/test_main.py`：

```python
import main


def test_build_parser_has_subcommands():
    parser = main.build_parser()
    args = parser.parse_args(["ingest"])
    assert args.command == "ingest"
    args = parser.parse_args(["ask", "预算多少"])
    assert args.command == "ask"
    assert args.question == "预算多少"
```

- [ ] **Step 2: 运行验证失败**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_main.py -q`
Expected: FAIL（`main` 模块不存在）。

- [ ] **Step 3: 实现 `main.py`**

```python
"""Command-line entrypoint: ingest, ask."""
from __future__ import annotations

import argparse
import asyncio

import _bootstrap
_bootstrap.ensure_wkc()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai-procurement-intelligence")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ingest", help="抓取云南政府采购网公告并入库")
    ask_p = sub.add_parser("ask", help="基于知识库问答")
    ask_p.add_argument("question", help="要提问的内容")
    return parser


async def _ask(question: str) -> None:
    from ask import ask
    from store import Store

    store = Store()
    result = await ask(question, store)
    print(result["answer"])
    for src in result["sources"]:
        print(f"- {src}")


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "ask":
        asyncio.run(_ask(args.question))
    elif args.command == "ingest":
        from ingest import ingest
        from source import yngp_pages
        from store import Store

        counts = asyncio.run(ingest(yngp_pages(), Store()))
        print(counts)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 运行验证通过**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_main.py -q`
Expected: PASS（1 passed）。

- [ ] **Step 5: 提交**

```powershell
git add main.py tests/test_main.py
git commit -m "feat: CLI entrypoint for ingest and ask"
```

---

### Task 10: 全量回归与冒烟

**Files:**
- Verify: 全部测试模块
- Do not modify: `web_keyword_catcher` 任何文件

- [ ] **Step 1: 运行全部单元测试**

Run: `.\.venv\Scripts\python.exe -m pytest tests -q`
Expected: 全部通过（config 3 + clean 4 + chunk 3 + embed 5 + store 3 + source 2 + ingest 3 + ask 2 + main 1 = 26 passed）。

- [ ] **Step 2: 手动冒烟（真实环境，不入 CI）**

```powershell
$env:DEEPSEEK_API_KEY="<你的 key>"
$env:EMBEDDING_API_KEY="<embedding key>"
$env:EMBEDDING_MODEL="bge-large-zh"
.\.venv\Scripts\python.exe main.py ingest
.\.venv\Scripts\python.exe main.py ask "近期有哪些设备采购项目？"
```

Expected: `ingest` 打印 `{"documents": N, "chunks": M, "errors": K}`；`ask` 打印答案 + 来源 URL 列表。

- [ ] **Step 3: 检查仓库范围**

Run: `git status --short`
Expected: 仅有本计划创建的新项目文件；`web_keyword_catcher` 目录内无任何改动。

- [ ] **Step 4: 报告证据**

报告精确测试数、真实 `ingest`/`ask` 输出、入库文档数、以及真实抓取中 `crawler.crawl` 签名是否需要微调（见 Task 6 说明）。

---

## 自审记录

- **规格覆盖**：spec 第 6 节组件（ingest/clean/chunk/embed/store/ask/main）→ Task 2~9；第 8 节错误处理 → Task 7 失败隔离、Task 2/4 降级；第 9 节测试 → 每任务测试；第 10 节完成标准 → Task 10。
- **占位符**：无 TBD/TODO；各任务含完整代码与命令。
- **类型一致性**：`Chunk` 字段（text/url/title/published_date/index）在 Task 3 定义，Task 5/7/8 引用一致；`Store` 方法（save_document/upsert_chunks/query）在 Task 5 定义，Task 7/8 调用一致；`embed` 签名 `embed(list[str]) -> list[list[float]]` 在各处一致；`_bootstrap.ensure_wkc()` 在 source/ask/main 一致。
