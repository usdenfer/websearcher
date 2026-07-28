"""Locate-view builder: rewrite a fetched page with obvious keyword highlighting.

The original page is sanitized (scripts, inline handlers and CSP meta are
removed), a <base> tag keeps relative resources working, and a small
toolbar + highlighter script is injected so the keyword is impossible to
miss: every occurrence is marked, the first one is scrolled into view,
and the toolbar offers prev/next navigation.
"""
from __future__ import annotations

import json

from bs4 import BeautifulSoup

_LOCATE_STYLE = """
mark.__kw { background:#ffe58f; outline:2px solid #faad14;
  border-radius:2px; padding:0 1px; }
mark.__kw.__active { background:#ff9c6e; outline:3px solid #d4380d; }
#__kwloc { position:fixed; top:0; left:0; right:0; z-index:2147483647;
  background:#1f1f1f; color:#fff; font:14px/1.6 "Microsoft YaHei",sans-serif;
  padding:8px 14px; display:flex; gap:10px; align-items:center;
  box-shadow:0 2px 8px rgba(0,0,0,.45); }
#__kwloc .__info { flex:1; }
#__kwloc button { font:13px/1 "Microsoft YaHei",sans-serif; padding:6px 12px;
  border:0; border-radius:6px; background:#2f6fed; color:#fff; cursor:pointer; }
#__kwloc button:disabled { opacity:.4; cursor:default; }
"""

_LOCATE_SCRIPT = r"""
(function () {
  var KW = __KW_JSON__;
  var marks = [];
  var walker = document.createTreeWalker(
    document.body, NodeFilter.SHOW_TEXT, {
      acceptNode: function (n) {
        if (!n.nodeValue || !n.nodeValue.trim()) return NodeFilter.FILTER_REJECT;
        var p = n.parentNode;
        if (!p) return NodeFilter.FILTER_REJECT;
        if (/^(SCRIPT|STYLE|NOSCRIPT)$/.test(p.nodeName)) return NodeFilter.FILTER_REJECT;
        if (p.closest && p.closest('#__kwloc')) return NodeFilter.FILTER_REJECT;
        return n.nodeValue.toLowerCase().indexOf(KW.toLowerCase()) >= 0
          ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
      }
    });
  var node, nodes = [];
  while ((node = walker.nextNode())) nodes.push(node);
  nodes.forEach(function (n) {
    var text = n.nodeValue, lower = text.toLowerCase(), kw = KW.toLowerCase();
    var frag = document.createDocumentFragment();
    var i = 0, idx;
    while ((idx = lower.indexOf(kw, i)) >= 0) {
      if (idx > i) frag.appendChild(document.createTextNode(text.slice(i, idx)));
      var m = document.createElement('mark');
      m.className = '__kw';
      m.textContent = text.slice(idx, idx + KW.length);
      frag.appendChild(m);
      marks.push(m);
      i = idx + KW.length;
    }
    frag.appendChild(document.createTextNode(text.slice(i)));
    n.parentNode.replaceChild(frag, n);
  });
  var bar = document.createElement('div');
  bar.id = '__kwloc';
  var info = document.createElement('span');
  info.className = '__info';
  var prev = document.createElement('button'); prev.textContent = '← 上一处';
  var next = document.createElement('button'); next.textContent = '下一处 →';
  var close = document.createElement('button'); close.textContent = '关闭';
  bar.appendChild(info); bar.appendChild(prev);
  bar.appendChild(next); bar.appendChild(close);
  document.body.appendChild(bar);
  var cur = -1;
  function show(i) {
    if (!marks.length) return;
    cur = (i + marks.length) % marks.length;
    marks.forEach(function (m) { m.classList.remove('__active'); });
    marks[cur].classList.add('__active');
    marks[cur].scrollIntoView({ block: 'center', behavior: 'smooth' });
    info.textContent = '关键词「' + KW + '」 第 ' + (cur + 1) + ' / ' + marks.length + ' 处';
  }
  prev.onclick = function () { show(cur - 1); };
  next.onclick = function () { show(cur + 1); };
  close.onclick = function () { bar.remove(); };
  if (marks.length) { show(0); }
  else {
    info.textContent = '页面中未找到「' + KW + '」（可能被隐藏或动态加载）';
    prev.disabled = next.disabled = true;
  }
})();
"""


def build_locate_page(html: str, page_url: str, keyword: str) -> str:
    """Return a sanitized copy of `html` with keyword-highlight injection."""
    soup = BeautifulSoup(html, "html.parser")

    # strip active content and anything that could break the locate view
    for tag in soup.find_all(["script", "noscript"]):
        tag.decompose()
    for meta in soup.find_all("meta"):
        if (meta.get("http-equiv") or "").lower() == "content-security-policy":
            meta.decompose()
    for tag in soup.find_all(True):
        for attr in [a for a in tag.attrs if a.lower().startswith("on")]:
            del tag[attr]

    # keep relative resources (css/images/links) working
    base = soup.new_tag("base", href=page_url)
    if soup.head:
        soup.head.insert(0, base)
        soup.head.append(soup.new_tag("style"))
        soup.head.find_all("style")[-1].string = _LOCATE_STYLE
    else:
        soup.insert(0, base)

    # JSON-encoding keeps the keyword a safe JS string; escape "</" so an
    # adversarial keyword cannot close the script element
    kw_json = json.dumps(keyword, ensure_ascii=False).replace("</", "<\\/")
    script = soup.new_tag("script")
    script.string = _LOCATE_SCRIPT.replace("__KW_JSON__", kw_json)
    if soup.body:
        soup.body.append(script)
    else:
        soup.append(script)

    return str(soup)
