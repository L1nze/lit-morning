"""个性化加权（参考 karpathy/arxiv-sanity-lite 的"点赞→推荐"思路，轻量纯标准库实现）：

`python bot.py like <id或链接>` 把点过赞的论文存进 data/likes.json；
之后每轮打分时，用点赞论文命中的关键词频次构成"兴趣画像"，
候选论文与画像重合越多，加分越高（最多 +6，不会喧宾夺主）。
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from .filtering import find_terms
from .paper import Paper

MAX_BOOST = 6


def likes_path(data_dir: Path) -> Path:
    return data_dir / "likes.json"


def load_likes(data_dir: Path) -> list[dict]:
    f = likes_path(data_dir)
    if not f.exists():
        return []
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_like(data_dir: Path, paper: Paper) -> int:
    """返回点赞总数。重复点赞幂等。"""
    likes = load_likes(data_dir)
    if any(x.get("id") == paper.id for x in likes):
        return len(likes)
    likes.append({
        "id": paper.id, "title": paper.title, "abstract": paper.abstract[:2000],
        "url": paper.url, "liked_at": datetime.now().isoformat(timespec="seconds"),
    })
    data_dir.mkdir(parents=True, exist_ok=True)
    likes_path(data_dir).write_text(
        json.dumps(likes, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(likes)


def remove_like(data_dir: Path, ref: str) -> bool:
    """按 id/URL/标题片段取消点赞，返回是否删掉了。"""
    from .cli import _norm_ref  # 局部导入避免循环依赖

    likes = load_likes(data_dir)
    q = _norm_ref(ref)
    kept = [x for x in likes
            if not (q == _norm_ref(str(x.get("id", "")))
                    or q == _norm_ref(str(x.get("url", "")))
                    or (q and q in (x.get("title", "") or "").lower()))]
    if len(kept) == len(likes):
        return False
    likes_path(data_dir).write_text(
        json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def build_profile(likes: list[dict], keywords: dict) -> Counter:
    """兴趣画像 = 点赞论文中各配置关键词的命中次数。"""
    profile: Counter = Counter()
    for like in likes:
        text = f"{like.get('title', '')}\n{like.get('abstract', '')}"
        for group in ("method_terms", "bonus_terms"):
            for term in find_terms(text, keywords.get(group, [])):
                profile[term] += 3 if group == "method_terms" else 1
    return profile


def boost_paper(p: Paper, profile: Counter) -> int:
    """按与画像的重合度加分，返回加了多少。"""
    if not profile:
        return 0
    text = f"{p.title}\n{p.abstract}"
    terms = {t for t in profile if find_terms(text, [t])}
    boost = min(sum(profile[t] for t in terms), MAX_BOOST)
    p.score += boost
    return boost
