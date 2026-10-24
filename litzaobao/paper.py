"""统一论文数据模型。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict


@dataclass
class Paper:
    id: str                    # 稳定 ID，如 "arxiv:2501.12345v1" / "doi:10.1000/xyz"
    source: str                # arxiv | openalex | crossref
    title: str
    authors: list[str] = field(default_factory=list)
    abstract: str = ""
    url: str = ""
    published: str = ""        # ISO 日期 "2026-10-15"
    doi: str = ""
    journal: str = ""
    journal_tier: int = 3       # 1 顶刊 / 2 领域权威 / 3 普通（见 journals.py）
    journal_rank: dict | None = None   # 中科院/JCR/IF/Top 分区数据（见 journal_rank.py）
    score: int = 0
    matched: list[str] = field(default_factory=list)   # 命中的关键词
    summary_zh: str = ""       # LLM 生成的中文三句话摘要
    highlight: str = ""        # 一句话说明为什么和你相关

    @property
    def short_authors(self) -> str:
        if not self.authors:
            return "未知作者"
        first = self.authors[0]
        if len(self.authors) == 1:
            return _clean_name(first)
        if len(self.authors) == 2:
            return f"{_clean_name(first)} & {_clean_name(self.authors[1])}"
        return f"{_clean_name(first)} 等 {len(self.authors)} 人"

    @property
    def dedup_key(self) -> str:
        """标题归一化作为跨源去重键（同论文常被多个库收录）。"""
        norm = re.sub(r"[^a-z0-9]+", "", self.title.lower())
        return norm[:120] if norm else self.id

    def to_dict(self) -> dict:
        return asdict(self)


def _clean_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip())
