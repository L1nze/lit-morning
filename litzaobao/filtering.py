"""关键词打分：主题词必须命中（可关），方法词/加分词累加，词边界正则匹配。"""
from __future__ import annotations

import re

from .paper import Paper

_TERM_CACHE: dict[str, re.Pattern] = {}


def make_pattern(term: str) -> re.Pattern:
    """词边界 + 常见复数：agent/agents、benchmark/benchmarks、study/studies。"""
    if term not in _TERM_CACHE:
        t = term.lower()
        branches = [rf"{re.escape(t)}(?:e?s)?"]
        if t.endswith("y"):
            branches.append(rf"{re.escape(t[:-1])}ies")
        _TERM_CACHE[term] = re.compile(rf"\b(?:{'|'.join(branches)})\b", re.IGNORECASE)
    return _TERM_CACHE[term]


def find_terms(text: str, terms: list[str]) -> list[str]:
    hits = []
    for t in terms:
        if make_pattern(t).search(text):
            hits.append(t)
    return hits


def score_paper(p: Paper, keywords: dict,
                require_topic: bool = True, require_method: bool = False,
                min_score: int = 5) -> None:
    """就地打分：命中负向词 / 缺必备词 / score < min_score → score 置 0 表示淘汰。"""
    text = f"{p.title}\n{p.abstract}"
    if find_terms(text, keywords.get("exclude_terms", [])):
        p.score = 0
        return
    hit_t = find_terms(text, keywords["topic_terms"])
    hit_m = find_terms(text, keywords["method_terms"])
    hit_x = find_terms(text, keywords["bonus_terms"])
    if require_topic and not hit_t:
        p.score = 0
        return
    if require_method and not hit_m:
        p.score = 0
        return
    p.score = 3 * len(hit_t) + 2 * len(hit_m) + 1 * len(hit_x)
    p.matched = hit_t + hit_m + hit_x
    if p.score < min_score:
        p.score = 0


def dedup_papers(papers: list[Paper]) -> list[Paper]:
    """跨源去重：优先保留信息更全（摘要更长/来源 arxiv）的记录。"""
    def quality(p: Paper) -> tuple:
        return (len(p.abstract), p.source == "arxiv", len(p.authors))
    by_key: dict[str, Paper] = {}
    for p in papers:
        key = p.dedup_key
        if key not in by_key or quality(p) > quality(by_key[key]):
            # 合并已知摘要，避免胜者缺摘要
            if key in by_key and not p.abstract and by_key[key].abstract:
                p.abstract = by_key[key].abstract
            by_key[key] = p
    return list(by_key.values())
