"""OpenAlex 数据源：标题+摘要检索，倒排索引重建摘要。免费额度按 IP 共享，
建议在 config.json 里填 openalex_email / openalex_api_key。"""
from __future__ import annotations

import json
from datetime import date

from ..paper import Paper
from ..web import http_get

API = "https://api.openalex.org/works"


def rebuild_abstract(inv: dict | None) -> str:
    """OpenAlex 的 abstract_inverted_index 是 {词: [位置...]}，还原成正文。"""
    if not inv:
        return ""
    n = max((pos for positions in inv.values() for pos in positions), default=-1) + 1
    words = [""] * n
    for word, positions in inv.items():
        for pos in positions:
            if pos < n:
                words[pos] = word
    return " ".join(w for w in words if w)


def _build_queries(keywords: dict) -> list[str]:
    queries: list[str] = []
    topic = (keywords.get("topic_terms") or [""])[0]
    for method in keywords["method_terms"][:4]:
        queries.append(f"{topic} {method}".strip())
    for bonus in keywords["bonus_terms"][:2]:
        queries.append(f"{topic} {bonus}".strip())
    queries.append(" ".join(keywords.get("topic_terms", [])[:2]))
    # 去重且保持顺序
    return list(dict.fromkeys(q for q in queries if q.strip()))[:6]


def _parse_results(text: str) -> list[dict]:
    data = json.loads(text)
    if "error" in data:
        raise RuntimeError(f"OpenAlex: {data.get('error')} - {data.get('message', '')[:120]}")
    return data.get("results", [])


def fetch(cutoff: date, keywords: dict, email: str = "", api_key: str = "") -> list[Paper]:
    params_base: dict = {"per-page": "25", "sort": "publication_date:desc"}
    if email:
        params_base["mailto"] = email
    if api_key:
        params_base["api_key"] = api_key

    by_id: dict[str, Paper] = {}
    for q in _build_queries(keywords):
        params = dict(params_base)
        params["filter"] = (
            f"from_publication_date:{cutoff.isoformat()},"
            f"title_and_abstract.search:{q}"
        )
        for item in _parse_results(http_get(API, params=params)):
            oa_id = item.get("id", "").rsplit("/", 1)[-1]
            if not oa_id or oa_id in by_id:
                continue
            published = item.get("publication_date") or ""
            if not published:
                continue
            doi = (item.get("doi") or "").replace("https://doi.org/", "")
            authors = [a.get("author", {}).get("display_name", "")
                       for a in item.get("authorships", [])[:12] if a.get("author")]
            by_id[oa_id] = Paper(
                id=f"openalex:{oa_id}",
                source="openalex",
                title=item.get("display_name", "").strip(),
                authors=authors,
                abstract=rebuild_abstract(item.get("abstract_inverted_index")),
                url=item.get("doi") or item.get("id", ""),
                published=published,
                doi=doi,
                journal=((item.get("primary_location") or {}).get("source") or {})
                .get("display_name") or "",
            )
    return list(by_id.values())
