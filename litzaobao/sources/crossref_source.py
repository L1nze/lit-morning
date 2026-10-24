"""Crossref 数据源：期刊文章元数据（含部分摘要，JATS XML 需清洗）。
无 Key 可用，配 crossref_email 进礼貌池更稳。"""
from __future__ import annotations

import json
import re
from datetime import date

from ..paper import Paper
from ..web import http_get

API = "https://api.crossref.org/works"

_JATS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def clean_jats(text: str) -> str:
    return _WS.sub(" ", _JATS.sub(" ", text or "")).strip()


def _build_queries(keywords: dict) -> list[str]:
    base = []
    topic = (keywords.get("topic_terms") or [""])[0]
    for method in keywords["method_terms"][:3]:
        base.append(f"{topic} {method}".strip())
    return list(dict.fromkeys(q for q in base if q.strip()))[:3]


def _parse(text: str) -> list[Paper]:
    data = json.loads(text)
    items = (data.get("message") or {}).get("items", [])
    papers = []
    for it in items:
        doi = (it.get("DOI") or "").strip()
        if not doi:
            continue
        title = it.get("title") or []
        title = title[0] if title else ""
        if not title:
            continue
        # 优先级 published-online > created(入库日) > issued(刊期)。
        # Elsevier 常无 published-online 且 issued 是未来刊期，created 最接近"上线时间"。
        date_parts = None
        for field in ("published-online", "created", "issued"):
            parts = ((it.get(field) or {}).get("date-parts") or [[None]])[0]
            if parts and parts[0]:
                date_parts = parts
                break
        if not date_parts:
            continue
        published = "-".join(f"{p:02d}" if i else str(p)
                             for i, p in enumerate(date_parts[:3]))
        authors = [f"{a.get('family', '')} {a.get('given', '')}".strip()
                   for a in it.get("author", [])[:12] if a.get("family")]
        papers.append(Paper(
            id=f"doi:{doi.lower()}",
            source="crossref",
            title=re.sub(r"\s+", " ", title).strip(),
            authors=authors,
            abstract=clean_jats(it.get("abstract", "")),
            url=it.get("URL") or f"https://doi.org/{doi}",
            published=published,
            doi=doi,
            journal=(it.get("container-title") or [""])[0],
        ))
    return papers


def fetch(cutoff: date, keywords: dict, email: str = "") -> list[Paper]:
    by_id: dict[str, Paper] = {}
    for q in _build_queries(keywords):
        params = {
            "query.bibliographic": q,
            "filter": f"from-pub-date:{cutoff.isoformat()},type:journal-article",
            "rows": "40",
            "select": "DOI,title,author,abstract,issued,published-online,created,URL,container-title",
        }
        if email:
            params["mailto"] = email
        for p in _parse(http_get(API, params=params)):
            by_id.setdefault(p.id, p)
    return list(by_id.values())
