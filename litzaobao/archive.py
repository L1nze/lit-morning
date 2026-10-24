"""历史归档：扫描 output/ 下 digest-*.json，生成 index.html 导航页与 RSS feed.xml。"""
from __future__ import annotations

import html as html_mod
import json
import re
from datetime import datetime, timezone, timedelta
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

_INDEX_DAYS = 31   # index.html 最多展示天数
_RSS_DAYS = 30     # feed.xml 最多条目数
_TZ = timezone(timedelta(hours=8))   # pubDate 固定东八区 08:30

_DAY_RE = re.compile(r"^digest-(\d{4}-\d{2}-\d{2})\.json$")

_CSS = """
:root { --bg:#f6f7f9; --card:#ffffff; --text:#1f2328; --muted:#6a737d;
        --accent:#0969da; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#0d1117; --card:#161b22; --text:#e6edf3; --muted:#8b949e;
          --accent:#58a6ff; } }
* { box-sizing: border-box; }
body { margin:0; padding:24px 12px; background:var(--bg); color:var(--text);
       font-family:-apple-system,"Segoe UI","Microsoft YaHei","PingFang SC",sans-serif;
       line-height:1.65; }
.wrap { max-width:820px; margin:0 auto; }
h1 { font-size:1.35em; margin:0 0 4px; }
.sub { color:var(--muted); font-size:.9em; margin-bottom:16px; }
.sub a { color:var(--accent); text-decoration:none; }
.sub a:hover { text-decoration:underline; }
.day { background:var(--card); border-radius:10px; padding:16px 18px; margin-bottom:14px;
       box-shadow:0 1px 2px rgba(0,0,0,.06); }
.day h2 { font-size:1.05em; margin:0 0 8px; }
.day h2 a { color:var(--accent); text-decoration:none; }
.day h2 a:hover { text-decoration:underline; }
.top { margin:0; padding-left:20px; }
.top li { margin:4px 0; font-size:.92em; }
.top a { color:var(--accent); text-decoration:none; }
.top a:hover { text-decoration:underline; }
footer { text-align:center; color:var(--muted); font-size:.78em; margin-top:24px; }
@media print { .day { box-shadow:none; border:1px solid #ddd; } }
"""


def _load_days(out_dir: Path, limit: int) -> list[tuple[str, list]]:
    """按日期降序读取 digest-*.json，返回 [(day, papers)]，最多 limit 天；
    命名或日期非法、JSON 坏掉的文件跳过（打印告警）。"""
    days: list[tuple[str, list]] = []
    for f in sorted(out_dir.glob("digest-*.json"), reverse=True):
        m = _DAY_RE.match(f.name)
        if not m:
            continue
        try:
            datetime.strptime(m.group(1), "%Y-%m-%d")   # 校验真实日期
            papers = json.loads(f.read_text(encoding="utf-8"))
            if not isinstance(papers, list):
                continue
        except (OSError, ValueError) as exc:
            print(f"[archive] 跳过无法解析的 {f.name}: {exc}")
            continue
        days.append((m.group(1), papers))
        if len(days) >= limit:
            break
    return days


def _pub_date(day: str) -> str:
    """当天 08:30 +0800 的 RFC 822 时间，如 Fri, 23 Oct 2026 08:30:00 +0800。"""
    dt = datetime.strptime(day, "%Y-%m-%d").replace(
        hour=8, minute=30, second=0, tzinfo=_TZ)
    return format_datetime(dt)


def _link(site_url: str, filename: str) -> str:
    """site_url 为空时退回相对文件名，否则拼接为绝对链接。"""
    if not site_url:
        return filename
    return f"{site_url.rstrip('/')}/{filename}"


def _cdata(text: str) -> str:
    """CDATA 包裹正文；内部的 ]]> 拆成两段 CDATA，防止提前闭合。"""
    return "<![CDATA[" + text.replace("]]>", "]]]]><![CDATA[>") + "]]>"


def write_index(out_dir: Path, site_url: str = "") -> Path | None:
    """生成历史归档 index.html：每天一张卡片（日期 · N 篇 + top3 标题）。
    没有日报文件或写入失败时返回 None，不抛出。"""
    try:
        days = _load_days(out_dir, _INDEX_DAYS)
        if not days:
            return None
        e = html_mod.escape
        sections = []
        for day, papers in days:
            href = f"digest-{day}.html"   # 与日报同目录，用相对路径
            titles = "".join(
                f'<li><a href="{e(href, quote=True)}">{e(p.get("title", ""))}</a></li>'
                for p in papers[:3]
                if isinstance(p, dict) and p.get("title"))
            sections.append(f"""
<section class="day">
  <h2><a href="{e(href, quote=True)}">{day} · {len(papers)} 篇</a></h2>
  <ol class="top">{titles}</ol>
</section>""")
        doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>lit-zaobao · 文献早报归档</title><style>{_CSS}</style></head>
<body><div class="wrap">
<h1>📚 lit-zaobao · 文献早报归档</h1>
<div class="sub">最近 {len(days)} 天 · <a href="feed.xml">RSS 订阅（feed.xml）</a></div>
{''.join(sections)}
<footer>lit-zaobao · 历史归档 · 每天一条，只推你方向的新论文</footer>
</div></body></html>"""
        out = out_dir / "index.html"
        out.write_text(doc, encoding="utf-8")
        return out
    except Exception as exc:   # 归档失败只告警，不影响主流程
        print(f"[archive] 写入 index.html 失败: {exc}")
        return None


def write_rss(out_dir: Path, site_url: str = "") -> Path | None:
    """生成 RSS 2.0 的 feed.xml：每天一个 item（top5 标题+亮点做摘要）。
    没有日报文件或写入失败时返回 None，不抛出。"""
    try:
        days = _load_days(out_dir, _RSS_DAYS)
        if not days:
            return None
        items = []
        for day, papers in days:
            link = _link(site_url, f"digest-{day}.html")
            lines = []
            for i, p in enumerate(
                    (p for p in papers[:5] if isinstance(p, dict)), 1):
                line = f"{i}. {p.get('title', '')}"
                if p.get("highlight"):
                    line += f" —— {p['highlight']}"
                lines.append(line)
            desc = "\n".join(lines)          # top5 标题 + highlight
            title = f"📚 文献早报 {day} · {len(papers)} 篇"
            items.append(
                "<item>"
                f"<title>{xml_escape(title)}</title>"
                f"<link>{xml_escape(link)}</link>"
                f"<guid>{xml_escape(link)}</guid>"
                f"<pubDate>{_pub_date(day)}</pubDate>"
                f"<description>{_cdata(desc)}</description>"
                "</item>")
        channel_link = site_url or "output/"
        rss = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<rss version="2.0"><channel>'
            '<title>lit-zaobao 文献早报</title>'
            f'<link>{xml_escape(channel_link)}</link>'
            '<description>论文日报 · 每天只推你方向的新文献 · 每日精选归档</description>'
            '<language>zh-CN</language>'
            + "".join(items) +
            '</channel></rss>\n')
        out = out_dir / "feed.xml"
        out.write_text(rss, encoding="utf-8")
        return out
    except Exception as exc:   # 同上：失败不抛出
        print(f"[archive] 写入 feed.xml 失败: {exc}")
        return None
