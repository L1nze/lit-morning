"""周报统计：聚合最近 N 天的日报 JSON，输出收录趋势、期刊结构与热词画像。"""
from __future__ import annotations

import json
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from .journals import BADGES

_SOURCE_LABEL = {"arxiv": "arXiv", "openalex": "OpenAlex", "crossref": "Crossref"}


def load_recent_digests(out_dir: Path, days: int) -> list[tuple[str, dict]]:
    """返回 [(日报日期, 单篇记录), ...]，只取最近 days 天。"""
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    rows: list[tuple[str, dict]] = []
    for f in sorted(out_dir.glob("digest-*.json")):
        day = f.stem.removeprefix("digest-")
        if day < cutoff:
            continue
        try:
            for rec in json.loads(f.read_text(encoding="utf-8")):
                rows.append((day, rec))
        except (json.JSONDecodeError, OSError):
            continue
    return rows


def build_weekly_markdown(rows: list[tuple[str, dict]], days: int) -> str:
    end = date.today().isoformat()
    if not rows:
        return f"最近 {days} 天（截至 {end}）没有日报数据，先跑 `python bot.py today --no-push`。"

    by_day: Counter = Counter(day for day, _ in rows)
    by_source: Counter = Counter(r.get("source", "?") for _, r in rows)
    by_tier: Counter = Counter(r.get("journal_tier", 3) for _, r in rows)
    by_journal: Counter = Counter(r.get("journal", "") for _, r in rows
                                  if r.get("journal"))
    by_term: Counter = Counter(t for _, r in rows for t in (r.get("matched") or []))

    lines = [f"## 📊 lit-zaobao 周报 · {end}（近 {days} 天）", ""]
    lines.append(f"共收录 **{len(rows)}** 篇，覆盖 {len(by_day)} 天。")
    lines.append("")
    lines.append("**每日收录**")
    for day in sorted(by_day):
        lines.append(f"- {day}: {by_day[day]} 篇")
    lines.append("")
    lines.append("**来源分布**: " + "、".join(
        f"{_SOURCE_LABEL.get(s, s)} {n}" for s, n in by_source.most_common()))
    lines.append("")
    lines.append("**期刊层次**: " + "、".join(
        f"{BADGES.get(t, '普通')} {n}" for t, n in sorted(by_tier.items())))
    lines.append("")
    ranked = [r for _, r in rows if (r.get("journal_rank") or {}).get("cas")]
    if ranked:
        by_cas = Counter(r["journal_rank"]["cas"] for r in ranked)
        lines.append("**中科院分区**: " + "、".join(
            f"{zone} {n}" for zone, n in by_cas.most_common()))
        lines.append("")
    top_journals = [j for j, _ in by_journal.most_common(8) if j]
    if top_journals:
        lines.append("**高频期刊**: " + "、".join(top_journals))
        lines.append("")
    lines.append("**热词 Top10**: " + "、".join(t for t, _ in by_term.most_common(10)))
    return "\n".join(lines)


def run_weekly(cfg, days: int = 7) -> str:
    out_dir = cfg.path("output_dir")
    rows = load_recent_digests(out_dir, days)
    md = build_weekly_markdown(rows, days)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"weekly-{date.today().isoformat()}.md"
    path.write_text(md, encoding="utf-8")
    print(md)
    print(f"\n已写入 {path}")
    return md
