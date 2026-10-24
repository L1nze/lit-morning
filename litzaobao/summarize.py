"""LLM 摘要：任意 OpenAI 兼容 /chat/completions 接口，多服务商可切换（见 llm_providers）。
无 api_key 或调用失败 → 自动降级为规则摘要（摘要前两句 + 命中词说明）。"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .llm_providers import chat_completion, resolve
from .paper import Paper
from .web import http_post

# 系统提示词模板：{focus} 由 config.json 的 research_focus 填充——
# 换个研究方向（任何专业）只改这一处即可，不用碰提示词本体。
DEFAULT_FOCUS = "大语言模型与智能体（LLM Agents）——示例方向，请在 config.json 改成你自己的"

SYSTEM_PROMPT_TEMPLATE = """你是「lit-zaobao 文献早报」的学术筛选助手，服务对象是一名研究「{focus}」的研究生。你的任务是把一批候选论文压缩成可信、可速览的中文日报。

对每篇论文输出两个字段：
1. summary_zh（三句话）：① 研究对象与场景（什么体系、什么工况）② 方法核心（写清关键信号/特征/模型；实验类写明测量方式与特征量）③ 结论或关键指标（只写摘要中真实出现的数字与结论）。
2. highlight（一句话）：判断它与「{focus}」的相关度与参考价值——是直接相关的方法、可借鉴的技巧，还是仅提供领域背景。相关性低就直说「弱相关」，不要硬凑价值。

硬规则：
- 只允许使用给定标题与摘要中的信息，禁止编造方法、数据或结论；摘要缺失时，summary_zh 以「（无摘要，仅凭标题推测）」开头并明确这是推测。
- overview 用 2-3 句中文概括这批文献的整体风向，并点名 1-2 篇最值得优先精读的（说明理由）。
- 严格输出 JSON，不输出任何其他文字、解释或 Markdown 围栏；items 与输入论文一一对应、顺序一致，id 原样回传。"""


def default_system_prompt(focus: str = "") -> str:
    """内置默认提示词：模板 + 研究方向（空则用内置示例方向）。"""
    return SYSTEM_PROMPT_TEMPLATE.replace("{focus}", (focus or "").strip() or DEFAULT_FOCUS)


# 兼容旧引用与既有测试的常量（默认方向渲染版）
DEFAULT_SYSTEM_PROMPT = default_system_prompt()


def effective_system_prompt(llm_raw: dict, research_focus: str = "") -> str:
    """当前实际生效的提示词：用户自定义 > 模板(研究方向) > 模板(默认方向)。"""
    custom = str((llm_raw or {}).get("system_prompt", "") or "").strip()
    return custom or default_system_prompt(research_focus)


def normalize_custom_prompt(text: str, research_focus: str = "") -> str:
    """设置框写回用：空或与默认模板一致 → 存空串（保持"跟随内置默认"）。"""
    text = (text or "").strip()
    if not text or text == default_system_prompt(research_focus).strip():
        return ""
    return text

_PROMPT_TMPL = """以下是 {n} 篇候选论文（编号/内部ID/标题/作者/摘要）：

{listing}

请输出 JSON：
{{
  "overview": "2-3 句中文总览：今天这批文献整体在讲什么、有没有值得优先读的",
  "items": [
    {{"id": "内部ID", "summary_zh": "3 句中文摘要：研究对象/方法/结论", "highlight": "一句话说明对研究方向的价值，无价值则写'弱相关'"}}
  ]
}}"""


def _listing(papers: list[Paper]) -> str:
    parts = []
    for i, p in enumerate(papers, 1):
        abstract = (p.abstract or "(无摘要，仅凭标题判断)").replace("\n", " ")[:900]
        parts.append(f"[{i}] ID={p.id}\n标题: {p.title}\n作者: {p.short_authors}\n摘要: {abstract}")
    return "\n\n".join(parts)


def extract_json(text: str) -> dict:
    """宽容解析：剥掉 ```json 围栏，截取首个 { 到末个 }。"""
    text = re.sub(r"```(?:json)?", "", text).strip().strip("`")
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("响应中找不到 JSON")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, dict) or "items" not in data:
        raise ValueError("JSON 结构不对")
    return data


def call_llm(papers: list[Paper], llm_raw: dict,
             system_prompt: str | None = None) -> tuple[str, dict[str, dict]]:
    """走四级兜底链（主模型→兜底模型→兜底服务商）调用 LLM。
    返回 (overview, {id: {summary_zh, highlight}})。异常向上抛。"""
    prompt = _PROMPT_TMPL.format(n=len(papers), listing=_listing(papers))
    sys_text = system_prompt or resolve(llm_raw).get("system_prompt") or DEFAULT_SYSTEM_PROMPT
    resp = chat_completion(llm_raw, {
        "temperature": 0.3,
        "messages": [
            {"role": "system", "content": sys_text},
            {"role": "user", "content": prompt},
        ],
    }, timeout=120)
    content = resp["choices"][0]["message"]["content"]
    data = extract_json(content)
    by_id: dict[str, dict] = {}
    for item in data.get("items", []):
        pid = str(item.get("id", "")).strip()
        if pid:
            by_id[pid] = {
                "summary_zh": str(item.get("summary_zh", "")).strip(),
                "highlight": str(item.get("highlight", "")).strip(),
            }
    return str(data.get("overview", "")).strip(), by_id


_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")


def fallback_summary(p: Paper) -> str:
    """无 LLM 时的规则摘要：摘要前两句（截断）。"""
    if not p.abstract:
        return "(该源未提供摘要，点击标题查看原文)"
    sentences = _SENT_SPLIT.split(p.abstract.strip())
    take = " ".join(sentences[:2])
    return (take[:400] + "…") if len(take) > 400 else take


def fallback_highlight(p: Paper) -> str:
    """无 LLM 时的一句话价值标注：只报告实际命中的关键词。"""
    return "命中: " + (", ".join(p.matched[:5]) if p.matched else "关键词")


def _load_cache(cache_path: Path | None) -> dict:
    """读取摘要缓存 {id: {summary_zh, highlight}}；不存在/损坏返回空 dict（坏了就重建）。"""
    if cache_path is None:
        return {}
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _overview_key(papers: list[Paper]) -> str:
    """总览缓存键：由这批论文 id 集合的 md5 决定——同一批全部命中时免调 LLM 取回总览。"""
    digest = hashlib.md5(",".join(sorted(p.id for p in papers)).encode("utf-8")).hexdigest()[:10]
    return f"__overview__:{digest}"


def _save_cache(cache_path: Path | None, cache: dict, entries: dict, overview_key: str,
                overview: str) -> None:
    """合并写回缓存（临时文件原子替换）；只存真实 LLM 结果。失败静默——缓存只是加速。"""
    if cache_path is None or not entries:
        return
    try:
        cache.update(entries)
        cache[overview_key] = {"overview": overview}
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(cache_path)
    except OSError:
        pass  # 磁盘满/权限问题不值得打断早报


def enrich(papers: list[Paper], llm_raw: dict, research_focus: str = "",
           cache_path: Path | None = None) -> str:
    """填充 summary_zh / highlight，返回 overview 文本。LLM 失败自动降级。
    cache_path 非空时启用本地缓存：命中的论文直接回填、不进 LLM 请求；
    只有真实 LLM 结果（含本次 overview）落盘复用，降级结果不缓存。"""
    resolved = resolve(llm_raw)
    resolved["system_prompt"] = effective_system_prompt(llm_raw, research_focus)
    if not resolved.get("api_key"):
        for p in papers:
            p.summary_zh, p.highlight = fallback_summary(p), fallback_highlight(p)
        return "（未配置 LLM，以下为规则生成的摘要；用 `python bot.py models` 查看可选服务商并填 key）"

    cache = _load_cache(cache_path)
    misses: list[Paper] = []       # 未命中的才送 LLM
    for p in papers:
        info = cache.get(p.id)
        if isinstance(info, dict) and info.get("summary_zh"):
            p.summary_zh = str(info["summary_zh"])
            p.highlight = str(info.get("highlight", ""))
        else:
            misses.append(p)

    if not misses:  # 全部命中缓存：不再调 LLM，总览按本批 id 指纹取回
        hit = cache.get(_overview_key(papers))
        overview = str((hit or {}).get("overview", "")).strip() if isinstance(hit, dict) else ""
        return overview or "（今日摘要复用本地缓存）"

    try:
        overview, by_id = call_llm(misses, llm_raw, system_prompt=resolved["system_prompt"])
        ordered = list(by_id.values())
        entries: dict[str, dict] = {}   # 本次可缓存的新结果（按论文真实 id 归档）
        for idx, p in enumerate(misses):
            # LLM 偶尔改写 ID：先按 ID 匹配，缺失时按位置兜底
            info = by_id.get(p.id) or (ordered[idx] if idx < len(ordered) else None)
            summary = str((info or {}).get("summary_zh") or "").strip()
            p.summary_zh = summary or fallback_summary(p)
            p.highlight = str((info or {}).get("highlight") or "").strip() or fallback_highlight(p)
            if summary:
                entries[p.id] = {"summary_zh": p.summary_zh, "highlight": p.highlight}
        _save_cache(cache_path, cache, entries, _overview_key(papers), overview)
        return overview or "（LLM 未返回总览）"
    except Exception as e:  # noqa: BLE001 - 降级保证早报照常发出（降级结果不落缓存）
        for p in misses:
            p.summary_zh, p.highlight = fallback_summary(p), fallback_highlight(p)
        return f"（LLM 调用失败已降级为规则摘要: {e}）"
