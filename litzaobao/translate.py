"""免费/低成本摘要翻译：多引擎 + 用户可选 + 自动回退 + 磁盘缓存。

引擎（参考 LitBoard 的免费翻译栈，并扩展）：
- llm       用当前 LLM 服务商翻译（复用已配 key，质量最好）
- volc      火山翻译（浏览器扩展公开接口，免 key）
- tencent   腾讯交互翻译（transmart 网页端公开接口，免 key）
- google    Google 免费端点（translate.googleapis.com，免 key；某些 IP 被限流）
- mymemory  MyMemory（免 key，每日免费配额，配邮箱可加大）
- deepl     DeepL API Free（需免费 key，50 万字符/月）
- baidu     百度翻译开放平台（需 appid+key，标准版免费）
- youdao    有道智云（需 appkey+secret，赠免费额度）

调用：translate_text(text, translate_cfg, llm_raw) → (译文, 实际引擎)。
先走用户选的 engine，失败按序回退到其余「可用」引擎。结果按文本哈希缓存。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.parse
from pathlib import Path

from .llm_providers import chat_completion, resolve as resolve_llm
from .web import http_get, http_post

# ---------------------------------------------------------------- 分块
_SENT = re.compile(r"(?<=[.!?;。！？；])\s+")


def split_chunks(text: str, limit: int) -> list[str]:
    """按句子边界分块，每块 ≤ limit 字符。"""
    text = (text or "").strip()
    if not text:
        return []
    chunks: list[str] = []
    buf = ""
    for sent in _SENT.split(text):
        if len(buf) + len(sent) + 1 > limit and buf:
            chunks.append(buf)
            buf = sent
        else:
            buf = f"{buf} {sent}".strip()
        while len(buf) > limit:  # 单句超长硬切
            chunks.append(buf[:limit])
            buf = buf[limit:]
    if buf:
        chunks.append(buf)
    return chunks


# ---------------------------------------------------------------- 各引擎
def _google(chunk: str, target: str, _cfg: dict, _llm: dict) -> str:
    text = http_get("https://translate.googleapis.com/translate_a/single", params={
        "client": "gtx", "sl": "auto", "tl": target, "dt": "t", "q": chunk}, retries=1)
    if text.lstrip().startswith("<"):  # 被限流时返回 HTML "Sorry" 页
        raise RuntimeError("Google 限流（当前 IP 被拦截）")
    data = json.loads(text)
    return "".join(seg[0] for seg in data[0] if seg and seg[0])


def _mymemory(chunk: str, target: str, cfg: dict, _llm: dict) -> str:
    params = {"q": chunk, "langpair": f"en|{target}"}
    if cfg.get("mymemory_email"):
        params["de"] = cfg["mymemory_email"]
    data = json.loads(http_get("https://api.mymemory.translated.net/get",
                               params=params, retries=1))
    out = (data.get("responseData") or {}).get("translatedText", "")
    if not out or "MYMEMORY WARNING" in out:
        raise RuntimeError("MyMemory 今日免费配额已用完")
    return out


def _deepl(chunk: str, target: str, cfg: dict, _llm: dict) -> str:
    data = json.loads(http_post("https://api-free.deepl.com/v2/translate", {
        "auth_key": cfg["deepl_api_key"], "text": chunk,
        "target_lang": "ZH" if target.startswith("zh") else target,
    }, form=True, timeout=30))
    return (data.get("translations") or [{}])[0].get("text", "")


def _baidu(chunk: str, target: str, cfg: dict, _llm: dict) -> str:
    salt = str(int(time.time() * 1000))
    sign = hashlib.md5(
        (cfg["baidu_appid"] + chunk + salt + cfg["baidu_key"]).encode()).hexdigest()
    data = json.loads(http_post("https://fanyi-api.baidu.com/api/trans/vip/translate", {
        "q": chunk, "from": "en", "to": "zh", "appid": cfg["baidu_appid"],
        "salt": salt, "sign": sign,
    }, form=True, timeout=30))
    if "error_code" in data:
        raise RuntimeError(f"百度翻译错误 {data.get('error_code')}: {data.get('error_msg', '')[:60]}")
    return "\n".join(x.get("dst", "") for x in data.get("trans_result", []))


def _youdao(chunk: str, target: str, cfg: dict, _llm: dict) -> str:
    salt = str(int(time.time() * 1000))
    curtime = str(int(time.time()))
    inp = chunk if len(chunk) <= 20 else chunk[:10] + str(len(chunk)) + chunk[-10:]
    sign = hashlib.sha256(
        (cfg["youdao_appkey"] + inp + salt + curtime + cfg["youdao_secret"]).encode()
    ).hexdigest()
    data = json.loads(http_post("https://openapi.youdao.com/api", {
        "q": chunk, "from": "en", "to": "zh-CHS", "appKey": cfg["youdao_appkey"],
        "salt": salt, "sign": sign, "signType": "v3", "curtime": curtime,
    }, form=True, timeout=30))
    if str(data.get("errorCode")) != "0":
        raise RuntimeError(f"有道翻译错误 {data.get('errorCode')}")
    return "".join(data.get("translation") or [])


def _llm(chunk: str, target: str, _cfg: dict, llm_raw: dict) -> str:
    resp = chat_completion(llm_raw, {
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content":
             "将用户给出的英文学术摘要翻译为流畅、准确的中文，保留专业术语"
             "（如 Transformer、RLHF、思维链），只输出译文本身。"},
            {"role": "user", "content": chunk}]}, timeout=60)
    return resp["choices"][0]["message"]["content"].strip()


def _volc(chunk: str, target: str, _cfg: dict, _llm: dict) -> str:
    """火山翻译：浏览器扩展公开接口，免 key。"""
    data = json.loads(http_post(
        "https://translate.volcengine.com/crx/translate/v1/",
        {"source_language": "auto",
         "target_language": "zh" if target.startswith("zh") else target,
         "text": chunk}, timeout=30))
    out = str(data.get("translation") or "").strip()
    if not out:
        raise RuntimeError("火山翻译未返回结果")
    return out


_TENCENT_CLIENT_KEY = ("browser-chrome-110.0.0-Mac OS-df4bd4c5-a65d-44b2-a40f"
                       "-42f34f3535f2-1677486696487")  # transmart 网页端公开固定值


def _tencent(chunk: str, target: str, _cfg: dict, _llm: dict) -> str:
    """腾讯交互翻译：transmart.qq.com 网页端接口，免 key（公开 client_key）。"""
    data = json.loads(http_post(
        "https://transmart.qq.com/api/imt",
        {"header": {"fn": "auto_translation", "client_key": _TENCENT_CLIENT_KEY},
         "type": "plain", "model_category": "normal",
         "source": {"lang": "auto", "text_list": [chunk]},
         "target": {"lang": "zh" if target.startswith("zh") else target}},
        timeout=30, headers={
            "Referer": "https://transmart.qq.com/zh-CN/index",
            "Origin": "https://transmart.qq.com"}))
    rows = data.get("auto_translation")
    if not isinstance(rows, list):
        raise RuntimeError("腾讯交互翻译返回了无法解析的结果")
    out = "\n".join(r for r in rows if r).strip()
    if not out:
        raise RuntimeError("腾讯交互翻译未返回结果")
    return out


# id → {名称, 分块上限, 所需配置, 函数}
ENGINES: dict[str, dict] = {
    "llm":      {"name": "当前 LLM 服务商", "chunk": 2500, "needs": [], "fn": _llm},
    "volc":     {"name": "火山翻译（免费）", "chunk": 4000, "needs": [], "fn": _volc},
    "tencent":  {"name": "腾讯交互翻译（免费）", "chunk": 1500, "needs": [], "fn": _tencent},
    "google":   {"name": "Google 免费版", "chunk": 1400, "needs": [], "fn": _google},
    "mymemory": {"name": "MyMemory 免费版", "chunk": 450, "needs": [], "fn": _mymemory},
    "deepl":    {"name": "DeepL API Free", "chunk": 3000, "needs": ["deepl_api_key"], "fn": _deepl},
    "baidu":    {"name": "百度翻译", "chunk": 1500, "needs": ["baidu_appid", "baidu_key"], "fn": _baidu},
    "youdao":   {"name": "有道智云", "chunk": 1500, "needs": ["youdao_appkey", "youdao_secret"], "fn": _youdao},
}
ENGINE_ORDER = ["llm", "volc", "tencent", "google", "mymemory", "deepl", "baidu", "youdao"]
_FALLBACK_ORDER = ENGINE_ORDER


def engine_configured(engine: str, cfg: dict, llm_raw: dict) -> bool:
    if engine == "llm":
        return bool(resolve_llm(llm_raw).get("api_key"))
    needs = ENGINES[engine]["needs"]
    return all(str(cfg.get(k, "")).strip() for k in needs)


def _run_engine(engine: str, text: str, cfg: dict, llm_raw: dict) -> str:
    spec = ENGINES[engine]
    parts = [spec["fn"](c, "zh-CN", cfg, llm_raw) for c in split_chunks(text, spec["chunk"])]
    out = "\n".join(p.strip() for p in parts if p.strip()).strip()
    if not out:
        raise RuntimeError(f"{spec['name']} 返回空译文")
    return out


def translate_text(text: str, translate_cfg: dict, llm_raw: dict) -> tuple[str, str]:
    """返回 (译文, 实际使用的引擎 id)。全部失败抛 RuntimeError(汇总原因)。"""
    active = str((translate_cfg or {}).get("engine", "llm") or "llm")
    if active not in ENGINES:
        active = "llm"
    chain = [active] + [e for e in _FALLBACK_ORDER if e != active]
    errors = []
    for engine in chain:
        if not engine_configured(engine, translate_cfg or {}, llm_raw):
            continue
        try:
            return _run_engine(engine, text, translate_cfg or {}, llm_raw), engine
        except Exception as e:  # noqa: BLE001 - 记录后回退下一引擎
            errors.append(f"{ENGINES[engine]['name']}: {str(e)[:80]}")
    raise RuntimeError("全部翻译引擎失败 → " + "；".join(errors) if errors
                       else "没有可用的翻译引擎（请在设置里配置）")


# ---------------------------------------------------------------- 缓存
def _cache_key(text: str) -> str:
    return hashlib.sha1(text.strip().encode()).hexdigest()


def cache_lookup(text: str, data_dir: Path) -> str:
    """只读缓存：有译文返回译文，没有返回空串（不触发翻译）。"""
    if not (text or "").strip():
        return ""
    cache_file = data_dir / "translation_cache.json"
    if not cache_file.exists():
        return ""
    try:
        cache = json.loads(cache_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ""
    return str((cache.get(_cache_key(text)) or {}).get("zh", ""))


def cached_translate(text: str, translate_cfg: dict, llm_raw: dict,
                     data_dir: Path) -> tuple[str, str]:
    """按文本哈希磁盘缓存，命中即返回；返回 (译文, 引擎或 'cache')。"""
    cache_file = data_dir / "translation_cache.json"
    cache: dict = {}
    if cache_file.exists():
        try:
            cache = json.loads(cache_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            cache = {}
    key = _cache_key(text)
    if key in cache:
        return cache[key]["zh"], "cache"
    zh, engine = translate_text(text, translate_cfg, llm_raw)
    cache[key] = {"zh": zh, "engine": engine, "src_len": len(text)}
    data_dir.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    return zh, engine
