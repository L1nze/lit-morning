"""摘要补全：Crossref/Scopus 常缺摘要，先 OpenAlex 按 DOI 批量取，
再用 Elsevier Abstract Retrieval 按 DOI 逐条兜底（同 LitBoard 的摘要回填思路）。
两者都失败则静默跳过，不影响早报。"""
from __future__ import annotations

import json
import time

from .paper import Paper
from .sources.openalex_source import rebuild_abstract
from .web import http_get

API = "https://api.openalex.org/works"
ELS_API = "https://api.elsevier.com/content/abstract/doi/{doi}"
BATCH = 50  # OpenAlex filter doi: 支持 OR 列表，50 个一批
ELS_MAX = 20  # Elsevier 逐条取，限制条数防配额消耗


def _norm_doi(doi: str) -> str:
    return (doi or "").strip().lower().removeprefix("https://doi.org/")


def _openalex_fill(need: list[Paper], email: str, api_key: str) -> int:
    filled = 0
    for i in range(0, len(need), BATCH):
        batch = need[i:i + BATCH]
        dois = "|".join(_norm_doi(p.doi) for p in batch)
        params: dict = {"filter": f"doi:{dois}", "per-page": str(len(batch)),
                        "select": "doi,abstract_inverted_index"}
        if email:
            params["mailto"] = email
        if api_key:
            params["api_key"] = api_key
        try:
            data = json.loads(http_get(API, params=params))
            if "error" in data:
                return filled
            by_doi = {_norm_doi(w.get("doi", "")): rebuild_abstract(w.get("abstract_inverted_index"))
                      for w in data.get("results", [])}
        except Exception:  # noqa: BLE001 - 补全失败无伤大雅
            return filled
        for p in batch:
            abstract = by_doi.get(_norm_doi(p.doi), "")
            if abstract:
                p.abstract = abstract
                filled += 1
    return filled


def _elsevier_fill(need: list[Paper], api_key: str) -> int:
    """Elsevier Abstract Retrieval：按 DOI 逐条兜底（限速 0.3s/条）。"""
    if not api_key:
        return 0
    filled = 0
    for p in need[:ELS_MAX]:
        if p.abstract:
            continue
        try:
            text = http_get(ELS_API.format(doi=_norm_doi(p.doi)),
                            params={"httpAccept": "application/json"},
                            headers={"X-ELS-APIKey": api_key}, retries=0)
            core = (json.loads(text).get("abstracts-retrieval-response") or {}).get("coredata") or {}
            desc = (core.get("dc:description") or "").strip()
            if desc:
                p.abstract = desc
                filled += 1
        except Exception:  # noqa: BLE001 - 单条失败跳过
            pass
        time.sleep(0.3)
    return filled


def enrich_abstracts(papers: list[Paper], email: str = "", api_key: str = "",
                     elsevier_key: str = "") -> int:
    """就地补全缺失摘要（OpenAlex 批量 → Elsevier 兜底），返回补全篇数。"""
    need = [p for p in papers if not p.abstract and _norm_doi(p.doi)]
    if not need:
        return 0
    filled = _openalex_fill(need, email, api_key)
    filled += _elsevier_fill([p for p in need if not p.abstract], elsevier_key)
    return filled

