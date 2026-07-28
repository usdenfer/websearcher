# 站内关键词搜索工具 — 设计文档

日期：2026-07-27
状态：已与用户确认
修订：2026-07-27 晚 — 搜索深度改为可配置（1~3 层，BFS 分层抓取，全局 60 页上限）；链接命中的目标地址解析为绝对地址并可直接点击。

## 1. 目标

一个本地网页工具：用户输入起始网址和关键词，工具抓取该页面及其同站一层子页面，搜索关键词（不限于正文文本，还包括图片及其他带关键词标记的元素），在独立工具窗口中展示命中位置与对应超链接；全部页面均未命中时明确提示关键词不存在。

## 2. 形态与技术选型

- 方案：Python FastAPI 后端 + 原生 HTML/JS 前端（单页面）。
- 后端同时托管前端静态文件，访问 `http://localhost:7100/` 即为独立工具窗口。
- 抓取与解析：httpx（异步）+ BeautifulSoup。
- 无数据库、无登录，搜索即搜即弃，不持久化结果。
- 项目根目录提供 `package.json`，`npm run dev` 脚本启动 Python 服务并转发 CLI 端口参数，用于预览生命周期管理。

## 3. 架构

```
server.py            FastAPI 应用：静态托管 + 搜索 API
crawler.py           抓取起始页、提取同站链接、并发抓取子页面
matcher.py           页面解析与关键词匹配（全部搜索位置）
static/index.html    前端单页面：输入、进度、结果列表、未命中提示
tests/               后端单元测试（本地 HTTP 夹具服务器）
```

接口：

- `GET /` → 前端页面
- `POST /api/search` → 请求 `{ startUrl, keywords, depth? }`（`depth` 1~3，默认 1）；同步执行完整搜索后一次性返回结果 JSON（深度 1 预计 10–40 秒，深度 2~3 可能 1–2 分钟，上限 60 页）。等待期间前端显示不确定进度动画（旋转指示 + "正在抓取页面…"文案），不做逐页进度上报。
- `GET /api/locate?url=...&keyword=...` → 抓取目标页，清除脚本/内联事件/CSP meta，注入 `<base>` 与高亮工具栏脚本后返回"高亮定位视图"：所有关键词醒目标出、自动滚动到第一处、工具栏显示"第 n/共 N 处"并支持上一处/下一处导航。用于替代浏览器自带 `#:~:text=`（隐藏内容失效、高亮不明显）。

结果 JSON 结构：

```json
{
  "startUrl": "https://example.com",
  "keywords": ["foo", "bar"],
  "pagesCrawled": 12,
  "pagesFailed": [{ "url": "...", "reason": "HTTP 404" }],
  "totalHits": 5,
  "results": [
    {
      "pageUrl": "https://example.com/about",
      "pageTitle": "About",
      "hits": [
        {
          "kind": "text | img-alt | img-src | title-attr | aria-label | link-text | link-href | meta | form",
          "snippet": "……关键词前后各约 60 字符上下文……",
          "keyword": "foo",
          "href": "https://example.com/about#:~:text=...",
          "linkHref": "命中的链接元素的 href（仅 link 类命中）"
        }
      ]
    }
  ]
}
```

## 4. 搜索流程

1. 校验起始 URL（必须 http/https）；校验搜索深度 `depth`（1~3，默认 1）。
2. 抓取起始页：超时 10 秒、跟随重定向、浏览器 UA 头。起始页失败 → 直接报错，不继续。
3. 从页面提取同域名链接：规范化（去锚点、去尾斜杠差异）、去重、排除二进制资源扩展（jpg/png/gif/svg/webp/pdf/zip/mp4 等）、仅 http/https；**已访问链接在计数前跳过**，每页最多取 30 个新链接。
4. BFS 分层抓取：`depth=1` 抓起始页的直接链接页；`depth=2/3` 继续抓下一层页面上的新链接。异步并发（并发度 8，每页超时 10 秒），**全局最多抓取 60 个页面**（含起始页）。
5. 对抓取成功且 Content-Type 为 HTML 的页面执行匹配；失败页面记入 `pagesFailed`，不影响整体流程。

## 5. 匹配规则

- 大小写不敏感；关键词为子串匹配（不拆词）。
- 多关键词：空格分隔，任一命中即计入该页面结果。
- 搜索位置与 `kind` 标签：

| kind | 位置 |
|---|---|
| `text` | 可见正文文本（排除 script/style/注释） |
| `img-alt` | 图片 `alt` |
| `img-src` | 图片 `src` 文件名部分 |
| `title-attr` | 任意元素 `title` 属性 |
| `aria-label` | `aria-label` 属性 |
| `link-text` | `<a>` 锚文本 |
| `link-href` | `<a>` 的 `href` |
| `meta` | meta keywords / description / og:* / twitter:* |
| `form` | input/textarea 的 `placeholder`、`value` |

- 每条命中生成上下文片段：关键词前后各约 60 字符，前端高亮关键词。
- 去重：同一页面、同一 kind、同一片段只保留一条。

## 6. 前端展示

- 顶部：起始 URL 输入框、关键词输入框、**搜索深度选择器（1~3 层，默认 1 层）**、搜索按钮；搜索中显示不确定进度动画（不做逐页进度上报）。
- 结果区按页面分组：组头为页面标题 + URL（可点击）；组内每条命中显示类型徽章、高亮片段、"跳转定位"按钮。
- 跳转定位：新标签打开目标页面。`text` 类命中在 URL 后附加 `#:~:text=<关键词URL编码>` 让浏览器自动滚动并高亮；**`link-text`/`link-href` 类命中直接跳转到该链接指向的目标页（正文页）**。`text` 与 `link-text` 命中额外提供"所在页定位"次级链接，打开 `/api/locate` 高亮定位视图（醒目样式 + 自动滚动 + 上一处/下一处导航）；其余类型直接打开所在页面。命中链接的 href 一律解析为绝对地址、可直接点击。
- 未命中：`totalHits = 0` 时显示醒目提示"关键词「X」在已成功抓取的 N 个页面中均未出现"，并列出已抓取页面清单。
- 抓取失败页面：底部折叠区列出 URL 与失败原因。

## 7. 异常处理

- URL 非法 → 前端即时提示，不发请求。
- 起始页不可达 / 非 HTML → 返回明确错误信息。
- 单个子页面超时、404、非 HTML → 记入 `pagesFailed`，不阻断。
- 后端总体超时保护：整个搜索请求 120 秒上限。

## 8. 测试

- 后端单元测试（pytest）：启动本地测试 HTTP 服务器挂载静态 HTML 夹具（含正文命中、img alt 命中、aria-label 命中、meta 命中、链接命中、无命中页面、404 页面），验证：
  - 同站链接提取与去重、二进制资源排除
  - 各 kind 的匹配规则
  - 未命中分支返回结构
  - `/api/search` 端到端返回结构
- 前端：浏览器手动验证输入校验、进度、分组结果、跳转定位、未命中提示。
