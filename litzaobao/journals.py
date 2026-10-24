"""期刊分级：把「顶刊 / 领域权威 / 普通」变成可配置的排序信号与徽章。

以下为示例映射（通用大刊 + AI/ML 名刊）；请在 config.json 的 journal_tiers
里用 {"期刊名": 1|2|3} 覆盖或新增，换成你自己领域的期刊。
"""
from __future__ import annotations

import re

# 1 = 顶刊（综合性大刊）；2 = 领域权威期刊；未列出 = 3（普通）
DEFAULT_TIERS: dict[str, int] = {
    # --- 顶刊 ---
    "nature": 1, "science": 1, "pnas": 1,
    "proceedings of the national academy of sciences": 1,
    "nature communications": 1, "nature machine intelligence": 1,
    # --- 领域权威（AI/ML，示例）---
    "journal of machine learning research": 2,
    "artificial intelligence": 2,
    "ieee transactions on pattern analysis and machine intelligence": 2,
}

BADGES = {1: "🏆 顶刊", 2: "⭐ 权威期刊"}

# 排序加分：顶刊 +4、权威 +2（不打门槛，只影响排序与展示）
SCORE_BONUS = {1: 4, 2: 2}

_WS = re.compile(r"[^a-z0-9]+")


def norm_name(name: str) -> str:
    """'Journal of Machine Learning Research' / 'journal-of-machine-learning-research' -> 全小写空格分隔。"""
    return _WS.sub(" ", (name or "").lower()).strip()


def journal_tier(journal: str, overrides: dict | None = None) -> int:
    n = norm_name(journal)
    if not n:
        return 3
    if overrides:
        for key, val in overrides.items():
            if norm_name(str(key)) == n and int(val) in (1, 2, 3):
                return int(val)
    return DEFAULT_TIERS.get(n, 3)


def badge(tier: int) -> str:
    return BADGES.get(tier, "")
