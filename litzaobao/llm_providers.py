"""LLM 服务商配置（学习 LitBoard agentcfg.js 的设计：服务商清单 + activeId）。

结构：
    "llm": {
      "active": "zhipu",
      "system_prompt": "",                 # 空 = 用代码内置默认
      "providers": {
        "zhipu":   {"api_key": "...", "model": "glm-4-flash"},
        "deepseek": {"api_key": "...", "model": "deepseek-chat"},
        "custom":  {"name": "自建", "base_url": "https://...", "api_key": "", "model": "m"}
      }
    }

- 内置预设（base_url + 候选模型）在代码里，config 只存用户填的差量（key/model/自定义端点）。
- 旧版扁平字段（llm.base_url/api_key/model）自动折进 id=custom 的服务商，零迁移成本。
- 状态只判「有没有」，有效性由 `models --test` 的连接测试回答（同 LitBoard 约定）。
"""
from __future__ import annotations

import json
from pathlib import Path

from .web import http_post

DEFAULT_PROVIDER_ID = "zhipu"

# id → {name, base_url, models, key_required}
PROVIDER_PRESETS: dict[str, dict] = {
    "zhipu": {
        "name": "智谱 GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models": ["glm-4-flash", "glm-4-flashx", "glm-4-air", "glm-4.5-air"],
        "key_required": True,
    },
    "zai": {
        "name": "Z.ai", "base_url": "https://api.z.ai/api/paas/v4",
        "models": ["glm-4.5-air", "glm-4-flash"],
        "key_required": True,
    },
    "deepseek": {
        "name": "DeepSeek", "base_url": "https://api.deepseek.com/v1",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "key_required": True,
    },
    "moonshot": {
        "name": "Kimi", "base_url": "https://api.moonshot.cn/v1",
        "models": ["moonshot-v1-8k", "moonshot-v1-32k", "kimi-latest"],
        "key_required": True,
    },
    "dashscope": {
        "name": "阿里百炼", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen-flash", "qwen-turbo", "qwen-plus"],
        "key_required": True,
    },
    "openai": {
        "name": "OpenAI", "base_url": "https://api.openai.com/v1",
        "models": ["gpt-4o-mini", "gpt-4o"],
        "key_required": True,
    },
    "openrouter": {
        "name": "OpenRouter", "base_url": "https://openrouter.ai/api/v1",
        "models": ["google/gemini-2.5-flash", "anthropic/claude-sonnet-4.5"],
        "key_required": True,
    },
    "siliconflow": {
        "name": "SiliconFlow", "base_url": "https://api.siliconflow.cn/v1",
        "models": ["Qwen/Qwen2.5-7B-Instruct", "deepseek-ai/DeepSeek-V3"],
        "key_required": True,
    },
    "ollama": {
        "name": "Ollama 本地", "base_url": "http://localhost:11434/v1",
        "models": ["qwen2.5:7b", "llama3.1:8b"],
        "key_required": False,
    },
    "custom": {
        "name": "自定义", "base_url": "",
        "models": [],
        "key_required": True,
    },
}

# id → 默认 embedding 模型（同为 OpenAI 兼容的 POST /embeddings）。
# 没列出的服务商 = 无公开 embedding API（如 DeepSeek），想用语义增强需在
# llm.embedding_model 显式指定一个可用端点的模型。
EMBEDDING_MODEL_PRESETS: dict[str, str] = {
    "zhipu": "embedding-3",
    "dashscope": "text-embedding-v3",
    "openai": "text-embedding-3-small",
    "siliconflow": "BAAI/bge-m3",
}


def all_providers(raw_llm: dict | None) -> dict[str, dict]:
    """预设 ⊕ 旧扁平字段(custom) ⊕ 用户 providers，返回完整服务商字典。"""
    raw_llm = raw_llm or {}
    out: dict[str, dict] = {}
    for pid, preset in PROVIDER_PRESETS.items():
        out[pid] = {"id": pid, "name": preset["name"], "base_url": preset["base_url"],
                    "api_key": "", "models": list(preset["models"]),
                    "model": preset["models"][0] if preset["models"] else "",
                    "fallback_model": "",
                    "key_required": preset["key_required"]}

    # 旧版扁平字段折进 custom（零迁移）
    legacy = {k: str(raw_llm.get(k, "") or "").strip() for k in ("base_url", "api_key", "model")}
    if any(legacy.values()):
        custom = out["custom"]
        custom.update({k: v for k, v in legacy.items() if v})
        if legacy["model"] and legacy["model"] not in custom["models"]:
            custom["models"] = [legacy["model"]]

    for pid, item in (raw_llm.get("providers") or {}).items():
        if not isinstance(item, dict):
            continue
        base = out.get(pid) or {"id": pid, "name": pid, "base_url": "", "api_key": "",
                                "models": [], "model": "", "fallback_model": "",
                                "key_required": True}
        for key in ("name", "base_url", "api_key", "model", "fallback_model"):
            val = str(item.get(key, "") or "").strip()
            if val:
                base[key] = val
        if isinstance(item.get("models"), list) and item["models"]:
            base["models"] = [str(m).strip() for m in item["models"] if str(m).strip()]
        if base["model"] and base["model"] not in base["models"]:
            base["models"].insert(0, base["model"])     # 用户手填的模型进清单
        out[pid] = base
    return out


def active_id(raw_llm: dict | None) -> str:
    providers = all_providers(raw_llm)
    wanted = str((raw_llm or {}).get("active", "") or "").strip()
    if wanted and wanted in providers:
        return wanted
    # 只有旧扁平配置时自动落在 custom
    if wanted not in providers and not (raw_llm or {}).get("providers"):
        if any(str((raw_llm or {}).get(k, "")).strip() for k in ("base_url", "api_key")):
            return "custom"
    return DEFAULT_PROVIDER_ID if DEFAULT_PROVIDER_ID in providers else next(iter(providers))


def resolve(raw_llm: dict | None) -> dict:
    """解析出当前生效的一组 {provider_id, name, base_url, api_key, model, system_prompt}。"""
    providers = all_providers(raw_llm)
    pid = active_id(raw_llm)
    p = providers[pid]
    if not p["model"] and p["models"]:
        p = dict(p, model=p["models"][0])
    return {
        "provider_id": pid,
        "name": p["name"],
        "base_url": p["base_url"],
        "api_key": p["api_key"],
        "model": p["model"],
        "fallback_model": str(p.get("fallback_model", "") or ""),
        "system_prompt": str((raw_llm or {}).get("system_prompt", "") or "").strip(),
    }


def provider_status(p: dict) -> str:
    """ready / partial / empty（只判有没有，不测有效性）。"""
    if not str(p.get("base_url", "")).strip():
        return "empty"
    if p.get("key_required", True) and not str(p.get("api_key", "")).strip():
        return "partial"
    if not str(p.get("model", "")).strip():
        return "partial"
    return "ready"


def write_active(config_path: Path, provider_id: str, model: str = "") -> None:
    """把 active（和可选 model）写回 config.json，原子替换。"""
    if not config_path.exists():
        raise SystemExit(f"配置文件不存在，先运行 `python bot.py init`")
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    llm = cfg.setdefault("llm", {})
    llm["active"] = provider_id
    provs = llm.setdefault("providers", {})
    entry = provs.setdefault(provider_id, {})
    if model:
        entry["model"] = model
    tmp = config_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(config_path)


def apply_llm_save(cfgd: dict, provider_id: str, *, set_active: bool = False,
                   base_url: str = "", model: str = "", api_key: str = "",
                   clear_key: bool = False, system_prompt: str | None = None,
                   models: list | None = None,
                   fallback_model: str | None = None,
                   fallback_provider: str | None = None) -> None:
    """设置框/ GUI 共用的写回逻辑（配合 config.update_config_file 使用）。"""
    llm = cfgd.setdefault("llm", {})
    if set_active:
        llm["active"] = provider_id
    entry = llm.setdefault("providers", {}).setdefault(provider_id, {})
    if base_url.strip():
        entry["base_url"] = base_url.strip()
    if model.strip():
        entry["model"] = model.strip()
    if clear_key:
        entry["api_key"] = ""
    elif api_key.strip():
        entry["api_key"] = api_key.strip()
    if system_prompt is not None:
        llm["system_prompt"] = system_prompt
    if models:
        entry["models"] = [str(m).strip() for m in models if str(m).strip()]
    if fallback_model is not None:      # 显式传空串 = 清除兜底
        entry["fallback_model"] = fallback_model.strip()
    if fallback_provider is not None:   # 显式传空串 = 关闭兜底服务
        llm["fallback"] = {"provider_id": fallback_provider.strip()}


def resolve_fallback(raw_llm: dict | None) -> dict | None:
    """兜底服务商：llm.fallback = {"provider_id": ...}，模型跟随该服务商当前配置。"""
    raw_llm = raw_llm or {}
    fb = raw_llm.get("fallback") or {}
    pid = str(fb.get("provider_id", "") or "").strip()
    if not pid:
        return None
    providers = all_providers(raw_llm)
    p = providers.get(pid)
    if not p:
        return None
    if p.get("key_required", True) and not p["api_key"]:
        return None   # 没配 key 的服务商兜不了底
    model = str(fb.get("model", "") or "").strip() or p["model"]
    return {"provider_id": pid, "name": p["name"], "base_url": p["base_url"],
            "api_key": p["api_key"], "model": model,
            "fallback_model": str(p.get("fallback_model", "") or "")}


def chat_completion(llm_raw: dict, payload: dict, timeout: int = 60) -> dict:
    """POST /chat/completions 的四级兜底链：
    主模型 → 主兜底模型 → 兜底服务商模型 →（其兜底模型）。全部失败抛最后一个错误。"""
    def _post(resolved_like: dict, model: str) -> dict:
        return json.loads(http_post(
            resolved_like["base_url"].rstrip("/") + "/chat/completions",
            dict(payload, model=model), timeout=timeout,
            headers={"Authorization": f"Bearer {resolved_like.get('api_key', '')}"}))

    primary = resolve(llm_raw)
    fb = resolve_fallback(llm_raw)
    chain = [primary] + ([fb] if fb else [])
    last: Exception | None = None
    for res in chain:
        models = list(dict.fromkeys(
            m for m in (res["model"], str(res.get("fallback_model", "") or "")) if m))
        for model in models:
            try:
                return _post(res, model)
            except Exception as e:  # noqa: BLE001 - 限流/网络/模型不存在，一律试下一级
                last = e
    raise last or RuntimeError("LLM 调用失败")


def resolve_embedding_model(llm_raw: dict | None) -> str:
    """embedding 模型解析：llm.embedding_model 显式指定 > 服务商预设；空串 = 不可用。"""
    raw = llm_raw or {}
    explicit = str(raw.get("embedding_model", "") or "").strip()
    if explicit:
        return explicit
    return EMBEDDING_MODEL_PRESETS.get(active_id(raw), "")


def embeddings(llm_raw: dict, texts: list[str], timeout: int = 60) -> list[list[float]]:
    """POST /embeddings（OpenAI 兼容）：返回与 texts 等长、按 index 对齐的向量列表。"""
    if not texts:
        return []
    model = resolve_embedding_model(llm_raw)
    if not model:
        raise RuntimeError("当前服务商未配置 embedding 模型")
    resolved = resolve(llm_raw)
    resp = json.loads(http_post(
        resolved["base_url"].rstrip("/") + "/embeddings",
        {"model": model, "input": list(texts)}, timeout=timeout,
        headers={"Authorization": f"Bearer {resolved.get('api_key', '')}"}))
    # 服务商不保证按输入顺序返回，按 index 对齐
    rows = sorted(resp.get("data") or [], key=lambda d: int(d.get("index", 0)))
    vectors = [[float(x) for x in row.get("embedding") or []] for row in rows]
    if len(vectors) != len(texts):
        raise RuntimeError(f"embedding 返回 {len(vectors)} 条，与输入 {len(texts)} 条不符")
    return vectors


def fetch_models(base_url: str, api_key: str = "", timeout: int = 20) -> list[str]:
    """拉取服务商真实模型清单（OpenAI 兼容 GET /models）。"""
    from .web import http_get
    url = base_url.rstrip("/") + "/models"
    text = http_get(url, timeout=timeout, retries=1,
                    headers={"Authorization": f"Bearer {api_key}"} if api_key else {})
    data = json.loads(text)
    models = [m.get("id", "") for m in (data.get("data") or []) if m.get("id")]
    if not models:
        raise RuntimeError("该端点未返回任何模型（确认它实现了 GET /models）")
    return sorted(models)


def mask_key(key: str) -> str:
    """密钥展示掩码：渲染层永远拿不到明文（同 LitBoard 的 hasApiKey 占位约定）。"""
    key = str(key or "")
    if not key:
        return ""
    if len(key) <= 8:
        return "已保存(****)"
    return f"{key[:3]}…{key[-4:]}"


def test_connection(resolved: dict, timeout: int = 30) -> tuple[bool, str]:
    """最小对话请求验证连通性（同 LitBoard「测试连接」：只回答有效无效）。"""
    if not resolved.get("base_url"):
        return False, "base_url 未配置"
    if not resolved.get("model"):
        return False, "model 未配置"
    try:
        resp = json.loads(http_post(
            resolved["base_url"].rstrip("/") + "/chat/completions",
            {"model": resolved["model"], "max_tokens": 16,
             "messages": [{"role": "user", "content": "ping，回复 pong"}]},
            timeout=timeout,
            headers={"Authorization": f"Bearer {resolved.get('api_key', '')}"}))
        text = str(resp["choices"][0]["message"]["content"])[:30]
        return True, f"连接成功，模型回复: {text}"
    except Exception as e:  # noqa: BLE001
        return False, f"连接失败: {str(e)[:160]}"
