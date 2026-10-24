"""arXiv 数据源：Atom API，按提交时间倒序抓近期预印本。"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date

from ..paper import Paper
from ..web import http_get

API = "https://export.arxiv.org/api/query"
NS = {"a": "http://www.w3.org/2005/Atom", "ax": "http://arxiv.org/schemas/atom"}


def build_query(keywords: dict) -> str:
    topic = " OR ".join(f'all:"{t}"' for t in keywords["topic_terms"])
    met = " OR ".join(f'all:"{t}"' for t in keywords["method_terms"])
    return f"({topic}) AND ({met})"


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def parse_atom(xml_text: str) -> list[Paper]:
    root = ET.fromstring(xml_text)
    papers: list[Paper] = []
    for entry in root.findall("a:entry", NS):
        raw_id = entry.findtext("a:id", default="", namespaces=NS)
        m = re.search(r"abs/([0-9.]+v\d+)$", raw_id)
        arxiv_id = m.group(1) if m else raw_id.rsplit("/", 1)[-1]
        published = (entry.findtext("a:published", default="", namespaces=NS) or "")[:10]
        papers.append(Paper(
            id=f"arxiv:{arxiv_id}",
            source="arxiv",
            title=_norm_ws(entry.findtext("a:title", default="", namespaces=NS)),
            authors=[_norm_ws(a.findtext("a:name", default="", namespaces=NS))
                     for a in entry.findall("a:author", NS)],
            abstract=_norm_ws(entry.findtext("a:summary", default="", namespaces=NS)),
            url=f"https://arxiv.org/abs/{arxiv_id}",
            published=published,
            journal="arXiv 预印本",
        ))
    return papers


def fetch(cutoff: date, keywords: dict, max_results: int = 100) -> list[Paper]:
    text = http_get(API, params={
        "search_query": build_query(keywords),
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "max_results": str(max_results),
    })
    papers = parse_atom(text)
    return [p for p in papers if p.published >= cutoff.isoformat()]
