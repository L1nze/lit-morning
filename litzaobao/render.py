"""报告渲染：Markdown（推送用）+ 独立 HTML（本地存档，深浅色自适应）。"""
from __future__ import annotations

import html as html_mod
import re
from datetime import date

from .paper import Paper
from .journals import badge as tier_badge
from .journal_rank import chips as rank_chips

_SOURCE_LABEL = {"arxiv": "arXiv", "openalex": "OpenAlex", "crossref": "期刊",
                 "scopus": "Scopus"}

DEFAULT_TAGLINE = "论文日报 · 每天只推你方向的新文献"


def _md_safe(text: str) -> str:
    """防止标题里的 []() 破坏 Markdown 链接结构（注入面）。"""
    return text.replace("[", "\\[").replace("]", "\\]").replace("\n", " ")


def _meta_line(p: Paper, include_rank: bool = True) -> str:
    from datetime import date as _date
    src = _SOURCE_LABEL.get(p.source, p.source)
    published = p.published
    if published and published > _date.today().isoformat():
        published += "（在版）"   # Scopus/Crossref 的在版文章刊期是未来日期
    bits = [src, published, f"匹配分 {p.score}"]
    if p.journal and src == "期刊":
        bits[0] = f"{src}·{p.journal[:40]}"
    head = []
    b = tier_badge(p.journal_tier)
    if b:
        head.append(b)
    if include_rank:
        head.extend(rank_chips(p.journal_rank))
    return " · ".join(head + bits)


# ---------------------------------------------------------------- Markdown
def render_markdown(papers: list[Paper], overview: str, day: date,
                    errors: list[str] | None = None,
                    tagline: str = DEFAULT_TAGLINE) -> str:
    lines = [f"## 📚 文献早报 {day.isoformat()}", ""]
    lines.append(f"今日为你筛出 **{len(papers)}** 篇「{tagline}」相关文献。")
    lines.append("")
    lines.append(f"> {overview}")
    lines.append("")
    for i, p in enumerate(papers, 1):
        lines.append(f"### {i}. [{_md_safe(p.title)}]({p.url})")
        lines.append("")
        lines.append(f"*{_meta_line(p)} · {p.short_authors}*")
        lines.append("")
        if p.highlight:
            lines.append(f"**⚡ {p.highlight}**")
            lines.append("")
        lines.append(p.summary_zh or "")
        lines.append("")
    if errors:
        lines.append("---")
        lines.append("数据源状态: " + "; ".join(errors))
    return "\n".join(lines)


# ---------------------------------------------------------------- HTML
_CSS = """
:root { --bg:#f6f7f9; --card:#ffffff; --text:#1f2328; --muted:#6a737d;
        --accent:#0969da; --tag:#ddf4e4; --tagtext:#116329; --warn:#fff8c5; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#0d1117; --card:#161b22; --text:#e6edf3; --muted:#8b949e;
          --accent:#58a6ff; --tag:#12261e; --tagtext:#7ee787; --warn:#292216; }
  mark { background:#5c4a00; color:#ffe89e; } }
* { box-sizing: border-box; }
body { margin:0; padding:24px 12px; background:var(--bg); color:var(--text);
       font-family:-apple-system,"Segoe UI","Microsoft YaHei","PingFang SC",sans-serif;
       line-height:1.65; }
.wrap { max-width:820px; margin:0 auto; }
h1 { font-size:1.35em; margin:0 0 4px; }
.sub { color:var(--muted); font-size:.9em; margin-bottom:16px; }
.overview { background:var(--card); border:1px solid var(--muted); border-radius:10px;
            padding:12px 16px; margin-bottom:18px; font-size:.95em; }
.paper { background:var(--card); border-radius:10px; padding:16px 18px; margin-bottom:14px;
         box-shadow:0 1px 2px rgba(0,0,0,.06); }
.paper h2 { font-size:1.02em; margin:0 0 6px; }
.paper h2 a { color:var(--accent); text-decoration:none; }
.paper h2 a:hover { text-decoration:underline; }
.meta { color:var(--muted); font-size:.82em; margin-bottom:8px; }
.hl { border-left:3px solid var(--accent); padding-left:10px; margin:8px 0;
      font-size:.92em; }
.tags { margin-top:8px; }
.tag { display:inline-block; background:var(--tag); color:var(--tagtext);
       border-radius:999px; padding:1px 10px; font-size:.75em; margin:2px 4px 2px 0; }
.tier { display:inline-block; border:1px solid var(--accent); color:var(--accent);
        border-radius:6px; padding:0 8px; font-size:.78em; margin-right:6px; }
.tier1 { background:transparent; font-weight:600; }
.ranks { margin:-2px 0 8px; }
details { margin-top:8px; }
summary { cursor:pointer; color:var(--muted); font-size:.85em; }
.abs { font-size:.88em; color:var(--muted); }
.warn { background:var(--warn); border-radius:8px; padding:8px 12px;
        font-size:.82em; color:var(--muted); }
mark { background:#ffe89e; color:inherit; border-radius:3px; padding:0 2px; }
footer { text-align:center; color:var(--muted); font-size:.78em; margin-top:24px; }
@media print { .paper { box-shadow:none; border:1px solid #ddd; } }
"""


def _highlight_pattern(terms: list[str]) -> re.Pattern | None:
    """多个关键词合成一个词边界正则；复用 filtering.make_pattern 的复数词形
    （e?s / y→ies），长词优先排列，配合一次替换即可避免嵌套 <mark>。
    无有效词时返回 None（matched 为空的论文不处理）。"""
    cleaned = sorted({t.strip().lower() for t in terms if t and t.strip()},
                     key=len, reverse=True)
    branches: list[str] = []
    for t in cleaned:
        branches.append(rf"{re.escape(t)}(?:e?s)?")
        if t.endswith("y"):
            branches.append(rf"{re.escape(t[:-1])}ies")
    if not branches:
        return None
    return re.compile(rf"\b(?:{'|'.join(branches)})\b", re.IGNORECASE)


def _mark_hits(text: str, pattern: re.Pattern | None) -> str:
    """先整体 html.escape 再一次性加 <mark>：包住命中的原文片段（保留原
    大小写），不重新扫描替换结果，天然无嵌套标记。"""
    escaped = html_mod.escape(text)
    if pattern is None:
        return escaped
    return pattern.sub(lambda m: f"<mark>{m.group(0)}</mark>", escaped)


def render_html(papers: list[Paper], overview: str, day: date,
                errors: list[str] | None = None,
                tagline: str = DEFAULT_TAGLINE) -> str:
    e = html_mod.escape
    cards = []
    for i, p in enumerate(papers, 1):
        # 只高亮标题和英文摘要里的命中词；meta 行、中文摘要保持纯转义
        pattern = _highlight_pattern(p.matched[:8]) if p.matched else None
        tags = "".join(f'<span class="tag">{e(t)}</span>' for t in p.matched[:8])
        tier = (f'<span class="tier tier1">{e(tier_badge(p.journal_tier))}</span>'
                if tier_badge(p.journal_tier) else "")
        ranks = "".join(f'<span class="tier">{e(c)}</span>' for c in rank_chips(p.journal_rank))
        title_html = _mark_hits(p.title, pattern)
        abs_html = _mark_hits(p.abstract[:1500], pattern) if p.abstract else ""
        cards.append(f"""
<article class="paper">
  <h2>{tier}{i}. <a href="{e(p.url, quote=True)}" target="_blank" rel="noopener">{title_html}</a></h2>
  <div class="meta">{e(_meta_line(p, include_rank=False))} · {e(p.short_authors)}</div>
  {f'<div class="ranks">{ranks}</div>' if ranks else ''}
  {f'<div class="hl">⚡ {e(p.highlight)}</div>' if p.highlight else ''}
  <div class="abs">{e(p.summary_zh)}</div>
  {f'<details><summary>英文摘要</summary><p class="abs">{abs_html}</p></details>' if p.abstract else ''}
  <div class="tags">{tags}</div>
</article>""")
    err_html = (f'<div class="warn">⚠ 数据源状态：{e("; ".join(errors))}</div>'
                if errors else "")
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>文献早报 {day.isoformat()}</title><style>{_CSS}</style></head>
<body><div class="wrap">
<h1>📚 文献早报 {day.isoformat()}</h1>
<div class="sub">{e(tagline)} · 共 {len(papers)} 篇 · lit-zaobao 生成</div>
<div class="overview">{e(overview)}</div>
{err_html}
{''.join(cards)}
<footer>lit-zaobao · arXiv + OpenAlex + Crossref · 每天一条，只推你方向的新论文</footer>
</div></body></html>"""
