"""期刊分区查询：EasyScholar / OneScholar（SciGreat 接口）+ 本地缓存。

仿照 LitBoard 的「设置 → 集成与服务 → 期刊分区」：填一家服务商的 Key，
日报生成时按期刊名查中科院分区 / 新锐分区 / JCR 分区 / 影响因子 / Top，
随微信推送、HTML 日报和阅读页一起展示。

约定：
- 只查最终要推的那批文献（TopN），同一期刊一场只请求一次；
- 结果落盘 data/journal_rank_cache.json，默认 30 天有效；无数据的期刊 7 天内不重查；
- 网络/Key 失败只降级为「无分区数据」，不阻断早报；已有旧缓存沿用。
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
from pathlib import Path

from .web import http_get, http_post

EASYSCHOLAR_URL = "https://www.easyscholar.cc/open/getPublicationRank"
ONESCHOLAR_URL = "https://open.scigreat.com/api/v1/getrank"
ONESCHOLAR_LEGACY_URL = "https://api.scigreat.com/info/getrank"
# 官方公开 API 的网页版（OneScholar Rank 页面同款）；公开 API/旧接口都 404 时的兜底
ONESCHOLAR_WEB_URL = "https://www.scigreat.com/i/rank/s/getrank.php"

PROVIDERS: dict[str, dict] = {
    "easyscholar": {
        "name": "EasyScholar",
        "key_field": "easyscholar_key",
        "key_label": "SecretKey",
        "signup": "https://www.easyscholar.cc/console/user/open",
        "min_interval": 0.55,      # 官方免费额度 ≤2 次/秒
    },
    "onescholar": {
        "name": "OneScholar",
        "key_field": "onescholar_key",
        "key_label": "API Key",
        "signup": "https://www.scigreat.com/s/app/?t=oneapi",
        "min_interval": 0.55,      # 公开 API 限速 2 次/秒
    },
}

DEFAULTS = {
    "provider": "easyscholar",     # easyscholar | onescholar | off
    "easyscholar_key": "",
    "onescholar_key": "",
    "cache_days": 30,
}

# 兼容 LitBoard 的 "scigreat" 写法
_PROVIDER_ALIASES = {"scigreat": "onescholar", "sci_great": "onescholar",
                     "easy_scholar": "easyscholar"}

CACHE_FILE = "journal_rank_cache.json"


class RankError(RuntimeError):
    """分区服务返回错误（消息可直接展示给用户）。"""


# ------------------------------------------------------------------ 配置
def normalize_provider(value) -> str:
    p = str(value or "").strip().lower()
    p = _PROVIDER_ALIASES.get(p, p)
    return p if p in PROVIDERS or p == "off" else "easyscholar"


def settings(raw: dict) -> dict:
    """journal_rank 配置（与 DEFAULTS 合并，兼容没有该节的旧配置）。"""
    sec = raw.get("journal_rank") if isinstance(raw.get("journal_rank"), dict) else {}
    merged = {**DEFAULTS, **sec}
    merged["provider"] = normalize_provider(merged.get("provider"))
    return merged


def provider_name(provider: str) -> str:
    return PROVIDERS.get(normalize_provider(provider), {}).get("name", "未启用")


def provider_key(raw: dict, provider: str | None = None) -> str:
    s = settings(raw)
    provider = normalize_provider(provider or s["provider"])
    field = PROVIDERS.get(provider, {}).get("key_field", "")
    return str(s.get(field, "") or "").strip() if field else ""


def is_configured(raw: dict) -> bool:
    s = settings(raw)
    return s["provider"] != "off" and bool(provider_key(raw, s["provider"]))


def apply_rank_save(cfgd: dict, provider: str, key: str = "") -> None:
    """分区设置写回（配合 config.update_config_file 使用）；空 Key 保留原值。"""
    provider = normalize_provider(provider)
    section = cfgd.setdefault("journal_rank", {})
    section["provider"] = provider
    if provider != "off" and key.strip():
        section[PROVIDERS[provider]["key_field"]] = key.strip()


# ------------------------------------------------------------------ 字段归一
_WS = re.compile(r"\s+")
_CAS_PREFIX = re.compile(r"^(?:中科院(?:文献情报中心)?|CAS)\s*", re.IGNORECASE)
_QUARTILE = re.compile(r"^Q\s*([1-4])$", re.IGNORECASE)
_NEGATIVE = re.compile(r"(?:非|不是|未|无|not|no)", re.IGNORECASE)


def _text(value) -> str:
    if value is None or value is False:
        return ""
    if value is True:
        return "是"
    return _WS.sub(" ", str(value)).strip()


def _pick(merged: dict, *keys):
    """按顺序取第一个非空字段（0 与 False 视为有效值）。"""
    for key in keys:
        value = merged.get(key)
        if value is not None and value != "":
            return value
    return None


def _zone(value) -> str:
    text = _text(value)
    if not text:
        return ""
    text = _CAS_PREFIX.sub("", text)
    text = _WS.sub(" ", text)
    if re.fullmatch(r"[1-4]", text):
        return text + "区"
    match = re.fullmatch(r"([1-4])\s*区", text)
    return f"{match.group(1)}区" if match else text


def _quartile(value) -> str:
    text = _text(value)
    if not text:
        return ""
    match = _QUARTILE.match(text) or re.fullmatch(r"([1-4])", text)
    return f"Q{match.group(1)}" if match else text


def _flag(value) -> bool:
    if value is True:
        return True
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == 1
    text = _text(value)
    if not text or _NEGATIVE.search(text):
        return False
    return bool(re.search(r"(?:top|是|入选|收录|核心|yes|true)", text, re.IGNORECASE))


def _number(value):
    if value is None or value == "":
        return None
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def normalize(data: dict, provider: str = "") -> dict | None:
    """把 EasyScholar / OneScholar 的返回归一成统一的 rank 字典；无数据返回 None。

    两家结构相近：EasyScholar 是 {code, data: {officialRank: {all, select}}}，
    OneScholar 是 {code, results: [{data: {...}}]}；也兼容 data 直接平铺字段的版本。
    """
    if not isinstance(data, dict):
        return None
    official = data.get("officialRank") if isinstance(data.get("officialRank"), dict) else {}
    merged: dict = {**data}
    for part in ("all", "select"):
        node = official.get(part)
        if isinstance(node, dict):
            merged.update(node)

    rank = {
        "cas": _zone(_pick(merged, "sciBase", "sci_base", "cas", "cas_zone", "sciUp", "sci_up")),
        "cas_top": any(_flag(merged.get(k)) for k in
                       ("sciUpTop", "sci_up_top", "cas_top", "casTop")),
        "jcr": _quartile(_pick(merged, "sci", "jcr")),
        "imf": _number(_pick(merged, "sciif", "imf", "if")),
        "jci": _number(_pick(merged, "jci")),
        "xr": _zone(_pick(merged, "xr")),
        "xr_top": any(_flag(merged.get(k)) for k in ("xrTop", "xr_top")),
        "beihe": any(_flag(merged.get(k)) for k in
                     ("pku", "pku_core", "beida_core", "beihe", "core")),
        "esi": _text(_pick(merged, "esi")),
        "warning": _text(_pick(merged, "sciWarning", "sci_warning", "warning")),
    }
    rank = {k: v for k, v in rank.items() if v not in (None, "", False)}
    if not rank:
        return None
    if provider:
        rank["provider"] = normalize_provider(provider)
    return rank


def _fmt_number(value) -> str:
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return str(value)


def chips(rank: dict | None) -> list[str]:
    """分区标签：中科院 1区 · Top · JCR Q1 · IF 12.3。"""
    if not isinstance(rank, dict):
        return []
    out: list[str] = []
    if rank.get("cas"):
        out.append(f"中科院 {rank['cas']}")
    if rank.get("cas_top") or rank.get("xr_top"):
        out.append("Top")
    if rank.get("jcr"):
        out.append(f"JCR {rank['jcr']}")
    if rank.get("imf") is not None:
        out.append(f"IF {_fmt_number(rank['imf'])}")
    if rank.get("xr") and rank["xr"] != rank.get("cas"):
        out.append(f"新锐 {rank['xr']}")
    if rank.get("beihe"):
        out.append("北核")
    return out


def summary(rank: dict | None) -> str:
    return " · ".join(chips(rank))


# ------------------------------------------------------------------ 网络查询
def _loads(text: str, label: str) -> dict:
    try:
        data = json.loads(text or "{}")
    except json.JSONDecodeError:
        raise RankError(f"{label} 返回了无效 JSON") from None
    if not isinstance(data, dict):
        raise RankError(f"{label} 返回格式异常")
    return data


_ONESCHOLAR_ERRORS = {
    "invalid_api_key_provided": "OneScholar API Key 无效或已禁用（去 OneScholar API Console 重新生成）",
    "api_key_expired": "OneScholar API Key 已过期（去 OneScholar API Console 重新生成）",
    "invalid_authentication": "OneScholar 缺少或错误的 Authorization（检查 Key）",
    "rate_limit_exceeded": "OneScholar 请求过快（限 2 次/秒），稍后重试",
    "daily_limit_exceeded": "OneScholar 今日查询配额已用完",
    "too_many_items": "OneScholar 单次最多查询 20 个期刊",
}


def _post_onescholar(url: str, key: str, journal: str) -> dict:
    """OneScholar 公开 API（旧版 /info/getrank 同构）：返回 data 对象。"""
    try:
        text = http_post(url, [{"journal": [journal]}], timeout=20,
                         headers={"Authorization": f"Bearer {key}",
                                  "Accept": "application/json"})
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise RankError(f"OneScholar API Key 无效或无权限（HTTP {e.code}）") from None
        if e.code == 404:
            raise RankError("OneScholar 接口不可用（HTTP 404）") from None
        raise RankError(f"OneScholar 请求失败（HTTP {e.code}）") from None
    data = _loads(text, "OneScholar")
    if int(data.get("code") or 0) != 200:
        status = str(data.get("status") or "").strip()
        raise RankError(_ONESCHOLAR_ERRORS.get(status)
                        or str(data.get("message") or "OneScholar 未返回分区数据"))
    results = data.get("results") or []
    first = (results[0] if isinstance(results, list) and results
             and isinstance(results[0], dict) else {})
    return first["data"] if isinstance(first.get("data"), dict) else first


_TAG = re.compile(r"<[^>]+>")
_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_CELL = re.compile(r"<td[^>]*>(.*?)</td>", re.S)


def _cell_text(fragment: str) -> str:
    return _WS.sub(" ", _TAG.sub("", fragment or "")).strip()


def _first_number(text: str):
    match = re.search(r"\d+(?:\.\d+)?", text)
    return match.group(0) if match else ""


def _query_onescholar_web(journal: str) -> dict:
    """OneScholar 网页端点兜底：getrank.php 返回 HTML 表格，转成与公开 API 同构的 data。

    列：期刊名称 / ISSN / IF / JCR 分区 / 中科院分区 / 新锐分区 / 北大核心 / 南大核心。
    名称做子串匹配，行可能有多条，优先取与查询名完全一致（忽略大小写）的那行。
    """
    text = http_get(ONESCHOLAR_WEB_URL,
                    params={"cid": "sciweb", "tid": str(int(time.time() * 1000)),
                            "kw": journal},
                    timeout=20, retries=1,
                    headers={"Referer": "https://www.scigreat.com/i/rank/"})
    data = _loads(text, "OneScholar")
    code = int(data.get("code") or 0)
    if code == 201:
        return {}   # 网页端点约定：201 = 查询到 0 条（按「无数据」缓存，不报错）
    if code != 200:
        raise RankError("OneScholar 网页接口未返回分区数据")
    rows = [_CELL.findall(m.group(1)) for m in _ROW.finditer(str(data.get("data") or ""))]
    rows = [r for r in rows if len(r) >= 6]
    if not rows:
        return {}
    want = _WS.sub(" ", journal).strip().lower()
    cells = next((r for r in rows if _cell_text(r[0]).lower() == want), rows[0])
    jcr, cas_cell, xr_cell, beihe = (cells[i] for i in (3, 4, 5, 6))
    out = {
        "sci": _cell_text(jcr),
        "sciif": _first_number(_cell_text(cells[2])),
        "sciBase": _cell_text(cas_cell).replace("Top", "").strip(),
        "sciUpTop": "Top" in _cell_text(cas_cell),
        "xr": _cell_text(xr_cell).replace("Top", "").strip(),
        "xrTop": "Top" in _cell_text(xr_cell),
    }
    if _cell_text(beihe) not in ("-", ""):
        out["pku"] = _cell_text(beihe)
    return {k: v for k, v in out.items() if v not in ("", False)}


def query(provider: str, key: str, journal: str) -> dict:
    """真实网络查询，返回服务商的 data 对象；失败抛 RankError（供测试打桩）。"""
    provider = normalize_provider(provider)
    journal = (journal or "").strip()
    if not journal:
        raise RankError("期刊名为空")
    if provider == "easyscholar":
        text = http_get(EASYSCHOLAR_URL,
                        params={"secretKey": key, "publicationName": journal},
                        timeout=20, retries=1)
        data = _loads(text, "EasyScholar")
        if int(data.get("code") or 0) != 200 or not isinstance(data.get("data"), dict):
            raise RankError(str(data.get("msg") or "EasyScholar 未返回分区数据"))
        return data["data"]
    if provider == "onescholar":
        # 公开 API → 旧接口 → 网页端点：前两者 404（服务端故障/未上线）时逐级回退；
        # Key 无效（401/403）仍原样报错，提示用户去 Console 换 Key。
        for url in (ONESCHOLAR_URL, ONESCHOLAR_LEGACY_URL):
            try:
                return _post_onescholar(url, key, journal)
            except RankError as e:
                if "HTTP 404" not in str(e):
                    raise
        return _query_onescholar_web(journal)
    raise RankError("分区服务未启用")


_PACE_LOCK = threading.Lock()
_LAST_CALL: dict[str, float] = {}


def _pace(provider: str) -> None:
    interval = float(PROVIDERS.get(provider, {}).get("min_interval", 0.5))
    with _PACE_LOCK:
        wait = _LAST_CALL.get(provider, 0.0) + interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _LAST_CALL[provider] = time.monotonic()


def test_connection(raw: dict, provider: str | None = None, key: str | None = None,
                    journal: str = "Nature") -> tuple[bool, str]:
    """设置框「测试查询」：用 Nature 验证 Key（不入缓存）。"""
    s = settings(raw)
    provider = normalize_provider(provider or s["provider"])
    if provider == "off":
        return False, "分区服务已关闭（选 EasyScholar 或 OneScholar 并填 Key）"
    key = str(key or provider_key(raw, provider)).strip()
    spec = PROVIDERS[provider]
    if not key:
        return False, f"未填写 {spec['name']} {spec['key_label']}"
    try:
        rank = normalize(query(provider, key, journal), provider)
    except Exception as e:  # noqa: BLE001 - 测试接口只报可读原因
        return False, str(e)[:120]
    if not rank:
        return False, f"{spec['name']} 连接成功，但没有 {journal} 的分区数据（Key 可能无权限）"
    return True, f"{spec['name']} 连接成功 · {journal}: {summary(rank)}"


# ------------------------------------------------------------------ 缓存
def cache_path(data_dir: Path) -> Path:
    return data_dir / CACHE_FILE


def _journal_key(journal: str) -> str:
    return _WS.sub(" ", (journal or "").strip()).strip(" .,;").lower()


def _load_cache(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    entries = data.get("entries") if isinstance(data, dict) else None
    return entries if isinstance(entries, dict) else {}


def _save_cache(path: Path, entries: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": 1, "entries": entries},
                                  ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def _stamp(rank: dict | None, provider: str) -> dict | None:
    if not rank:
        return None
    out = dict(rank)
    out.setdefault("provider", provider)
    out["checked"] = time.strftime("%Y-%m-%d")
    return out


class JournalRankService:
    """一次运行内的分区查询器：内存去重 + 磁盘缓存 + 串行限速。"""

    def __init__(self, raw: dict, data_dir: Path):
        self.settings = settings(raw)
        self.provider = self.settings["provider"]
        self.key = provider_key(raw, self.provider)
        self.enabled = self.provider != "off" and bool(self.key)
        try:
            self.cache_days = max(0, int(self.settings.get("cache_days") or 0))
        except (TypeError, ValueError):
            self.cache_days = DEFAULTS["cache_days"]
        self.path = cache_path(data_dir)
        self.entries = _load_cache(self.path) if self.enabled else {}
        self.requests = 0
        self.hits = 0
        self.errors: dict[str, str] = {}
        self._memo: dict[str, dict | None] = {}
        self._dirty = False

    # ---- 查询 ----
    def _fresh(self, entry: dict) -> bool:
        try:
            age = time.time() - float(entry.get("at") or 0)
        except (TypeError, ValueError):
            age = 1e12
        days = self.cache_days if entry.get("ok") else min(self.cache_days, 7)
        return age < days * 86400

    def lookup(self, journal: str) -> dict | None:
        key = _journal_key(journal)
        if not key:
            return None
        if key in self._memo:
            return self._memo[key]
        entry = self.entries.get(key)
        if isinstance(entry, dict) and self._fresh(entry):
            self.hits += 1
            rank = entry.get("rank") if isinstance(entry.get("rank"), dict) else None
            self._memo[key] = rank
            return rank
        try:
            _pace(self.provider)
            self.requests += 1
            rank = _stamp(normalize(query(self.provider, self.key, journal), self.provider),
                          self.provider)
        except Exception as e:  # noqa: BLE001 - 单刊失败不该中断整轮
            self.errors[key] = str(e)[:160]
            # 网络抖动时沿用旧数据，总比推一条「无分区」好
            rank = entry.get("rank") if isinstance(entry, dict) else None
            self._memo[key] = rank if isinstance(rank, dict) else None
            return self._memo[key]
        self.entries[key] = {"at": time.time(), "ok": bool(rank),
                             "provider": self.provider, "rank": rank}
        self._dirty = True
        self._memo[key] = rank
        return rank

    def error_for(self, journal: str) -> str:
        return self.errors.get(_journal_key(journal), "")

    # ---- 批量标注 ----
    def annotate(self, papers) -> dict:
        stats = {"enabled": self.enabled, "provider": self.provider,
                 "total": 0, "hit": 0, "miss": 0, "error": 0,
                 "requests": 0, "cached": 0, "last_error": ""}
        if not self.enabled:
            return stats
        failed_before = len(self.errors)
        for paper in papers:
            journal = (getattr(paper, "journal", "") or "").strip()
            if not journal:
                continue
            stats["total"] += 1
            rank = self.lookup(journal)
            paper.journal_rank = rank
            stats["hit" if rank else "miss"] += 1
        stats["error"] = len(self.errors) - failed_before
        stats["requests"] = self.requests
        stats["cached"] = self.hits
        stats["last_error"] = next(iter(self.errors.values()), "")
        return stats

    def save(self) -> None:
        if self._dirty:
            _save_cache(self.path, self.entries)
            self._dirty = False


def annotate_papers(papers, raw: dict, data_dir: Path) -> dict:
    """管线入口：给这批文献标注 journal_rank，返回统计。"""
    service = JournalRankService(raw, data_dir)
    if not service.enabled:
        return {"enabled": False, "provider": service.provider, "total": 0, "hit": 0,
                "miss": 0, "error": 0, "requests": 0, "cached": 0, "last_error": ""}
    stats = service.annotate(papers)
    service.save()
    return stats
