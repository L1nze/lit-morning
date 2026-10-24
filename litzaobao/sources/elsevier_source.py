"""Elsevier Scopus 数据源（参照 LitBoard：与 OpenAlex 互补，引用数据更权威）。
需 elsevier_api_key（https://dev.elsevier.com 免费申请）；未配置时静默跳过。
Scopus 检索不含摘要，摘要由 enrich 阶段补。"""
from __future__ import annotations

import json
import time
from datetime import date

from ..paper import Paper
from ..web import http_get

API = "https://api.elsevier.com/content/search/scopus"


def _build_queries(keywords: dict) -> list[str]:
    base = []
    topic = (keywords.get("topic_terms") or [""])[0]
    for method in keywords["method_terms"][:3]:
        base.append(f"TITLE-ABS-KEY({method} AND {topic})")
    return list(dict.fromkeys(base))[:3]


def _parse(text: str, cutoff: date) -> list[Paper]:
    data = json.loads(text)
    entries = (data.get("search-results") or {}).get("entry", [])
    papers = []
    for e in entries:
        title = (e.get("dc:title") or "").strip()
        published = (e.get("prism:coverDate") or "")[:10]
        if not title or not published or published < cutoff.isoformat():
            continue
        doi = (e.get("prism:doi") or "").strip()
        scopus_id = (e.get("dc:identifier") or "").replace("SCOPUS_ID:", "")
        papers.append(Paper(
            id=f"scopus:{scopus_id or doi}",
            source="scopus",
            title=title,
            authors=[e.get("dc:creator", "")] if e.get("dc:creator") else [],
            abstract="",  # Scopus 检索不含摘要
            url=f"https://doi.org/{doi}" if doi else e.get("prism:url", ""),
            published=published,
            doi=doi,
            journal=e.get("prism:publicationName", "") or "",
        ))
    return papers


def fetch(cutoff: date, keywords: dict, api_key: str = "") -> list[Paper]:
    if not api_key:
        return []  # 未配置 key：静默跳过（不算数据源故障）
    by_id: dict[str, Paper] = {}
    for i, q in enumerate(_build_queries(keywords)):
        if i:
            time.sleep(0.4)  # Elsevier 限速礼貌间隔
        text = http_get(API, params={"query": q, "count": "40", "sort": "-coverDate"},
                        headers={"X-ELS-APIKey": api_key, "Accept": "application/json"})
        for p in _parse(text, cutoff):
            by_id.setdefault(p.id, p)
    return list(by_id.values())
