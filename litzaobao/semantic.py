"""语义召回增强（纯标准库：embedding 走 OpenAI 兼容接口，余弦相似度手写）。

关键词打分会漏掉不写标准术语的相关论文，这里用向量补两层：
1. 画像加分：点赞论文（权重 1）+ research_focus（权重 2）的向量加权平均 = 兴趣质心，
   候选论文 title+abstract 与质心的余弦相似度分档加分（≥0.55 +2 / ≥0.65 +3 / ≥0.75 +4，
   语义部分封顶 +4），与现有关键词画像加分直接叠加；
2. 语义捞回：打分为 0 但 matched 非空的 borderline 论文，相似度 ≥0.60 的前
   rescue_top 篇以基础分 5 捞回候选列表（专捞"相关但没写标准术语"的漏网之鱼）。

启用开关 = embedding 模型能解析出来（llm.embedding_model 显式指定 > 服务商预设，
见 llm_providers.resolve_embedding_model）且当前服务商已配 API Key；
管线侧另有 semantic.enabled 总开关（默认开）。
向量缓存 data/embed_cache.json（key = sha256(text) 前 16 位，换 embedding 模型后
建议删掉重建），同文本不重复请求；写缓存走临时文件原子替换。
augment 是管线唯一入口，任何一步失败都降级返回原列表——早报不能因为语义增强挂掉。
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from . import llm_providers
from .paper import Paper
from .profile import load_likes

# 相似度 → 加分档位（从高到低取第一个命中的；语义加分封顶 +4）
BOOST_TIERS: list[tuple[float, int]] = [(0.75, 4), (0.65, 3), (0.55, 2)]
RESCUE_MIN_SIM = 0.60    # 捞回门槛：低于它多半只是关键词巧合
RESCUE_SCORE = 5         # 捞回论文的固定基础分（对齐默认 min_score）
DEFAULT_RESCUE_TOP = 2


def cosine(a: list[float], b: list[float]) -> float:
    """手写余弦相似度（fsum 累加保精度）；零向量或维度不一致 → 0。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = math.fsum(x * y for x, y in zip(a, b))
    norm = (math.sqrt(math.fsum(x * x for x in a))
            * math.sqrt(math.fsum(y * y for y in b)))
    return dot / norm if norm else 0.0


def _cache_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _cache_path(data_dir: Path) -> Path:
    return data_dir / "embed_cache.json"


def _load_cache(data_dir: Path) -> dict[str, list]:
    f = _cache_path(data_dir)
    if not f.exists():
        return {}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}    # 缓存坏了当没有，重拉一遍就是


def _embed_all(need: list[str], cache: dict, data_dir: Path, llm_raw: dict) -> None:
    """缺的文本合成一批请求，结果就地写回 cache 并持久化（临时文件原子替换）。"""
    if not need:
        return
    vectors = llm_providers.embeddings(llm_raw, need)
    for text, vec in zip(need, vectors):
        cache[_cache_key(text)] = [float(x) for x in vec]
    data_dir.mkdir(parents=True, exist_ok=True)
    path = _cache_path(data_dir)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _centroid(weighted: list[tuple[list[float], int]]) -> list[float]:
    """锚点向量的加权平均（点赞 1 / focus 2）。"""
    total = sum(w for _, w in weighted)
    dim = len(weighted[0][0])
    return [math.fsum(v[i] * w for v, w in weighted) / total
            for i in range(dim)]


def _boost_for(sim: float) -> int:
    for threshold, boost in BOOST_TIERS:
        if sim >= threshold:
            return boost
    return 0


def _rescue_top(llm_raw: dict) -> int:
    """捞回上限：llm.semantic_rescue_top 可覆盖（管线只传 llm 节，默认 2）。"""
    try:
        return max(0, int(llm_raw.get("semantic_rescue_top", DEFAULT_RESCUE_TOP)))
    except (TypeError, ValueError):
        return DEFAULT_RESCUE_TOP


def _text(p: Paper) -> str:
    return f"{p.title}\n{p.abstract}"


def augment(papers: list, rejected: list, focus: str, llm_raw: dict,
            data_dir: Path) -> tuple[list, str]:
    """管线入口（签名与调用方约定一致）：返回 (最终候选列表, 状态描述)。

    绝不抛异常：内部任何失败（网络/配置/缓存）都降级为原样返回 + 跳过说明。
    """
    try:
        return _augment(papers, rejected, focus, llm_raw or {}, Path(data_dir))
    except Exception as e:  # noqa: BLE001 - 语义增强失败不阻断早报
        return papers, f"跳过（{e}）"


def _augment(papers: list, rejected: list, focus: str,
             llm_raw: dict, data_dir: Path) -> tuple[list, str]:
    # 1) 启用判定：解析不出 embedding 模型（显式配置与服务商预设都没有）= 未启用；
    #    模型有但当前服务商没配 api_key 也直接未启用——避免每轮白发一次 401 请求
    if not llm_providers.resolve_embedding_model(llm_raw):
        return papers, "未启用（llm.embedding_model 未配置）"
    if not llm_providers.resolve(llm_raw).get("api_key"):
        return papers, "未启用（当前 LLM 服务商未配置 API Key）"
    rescue_top = _rescue_top(llm_raw)

    # 2) 画像锚点：点赞论文 title+abstract（权重 1）+ research_focus（权重 2）
    focus = str(focus or "").strip()
    anchors = [(f"{x.get('title', '')}\n{x.get('abstract', '')}", 1)
               for x in load_likes(data_dir)]
    anchors = [(t, w) for t, w in anchors if t.strip()]
    if focus:
        anchors.append((focus, 2))
    if not anchors:
        return papers, "未启用（无画像锚点：focus 与点赞均为空）"

    # 3) borderline 候选：打分为 0 但 matched 非空（调用方已筛过，这里再兜一层）；
    #    与候选列表同文的去重键直接排除，省一次多余的向量化
    candidates = list(papers or [])
    seen_keys = {p.dedup_key for p in candidates}
    borderline = [p for p in (rejected or [])
                  if getattr(p, "score", 0) == 0 and p.matched
                  and p.dedup_key not in seen_keys]

    # 4) 向量化：锚点 + 候选 + borderline 合并成一批，缓存缺啥补啥
    cache = _load_cache(data_dir)
    need: list[str] = []
    for text in ([t for t, _ in anchors] + [_text(p) for p in candidates]
                 + [_text(p) for p in borderline]):
        if _cache_key(text) in cache or text in need:
            continue
        need.append(text)
    _embed_all(need, cache, data_dir, llm_raw)

    def vec(text: str) -> list[float]:
        return cache.get(_cache_key(text)) or []

    # 5) 质心 + 候选加分（叠加在关键词分上，语义部分每篇最多 +4）
    center = _centroid([(vec(t), w) for t, w in anchors])
    boosted = 0
    for p in candidates:
        gain = _boost_for(cosine(vec(_text(p)), center))
        if gain:
            p.score += gain
            boosted += 1

    # 6) 语义捞回：按相似度降序取前 rescue_top 篇（sim ≥ 0.60），基础分 5 追加进候选
    sims = sorted(((cosine(vec(_text(p)), center), p) for p in borderline),
                  key=lambda x: x[0], reverse=True)
    rescued: list = []
    for sim, p in sims:
        if len(rescued) >= rescue_top or sim < RESCUE_MIN_SIM:
            break    # 已按降序排，后面的只会更低
        p.score = RESCUE_SCORE
        rescued.append(p)

    return candidates + rescued, (
        f"画像锚点 {len(anchors)} 条 · 加分 {boosted} 篇 · 捞回 {len(rescued)} 篇")
