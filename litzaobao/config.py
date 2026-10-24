"""配置加载：项目根目录 config.json，缺省值内置，改坏时给出可读报错。"""
from __future__ import annotations

import copy
import json
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path


def _project_root() -> Path:
    """源码运行时 = 包的上级目录；PyInstaller 打包(frozen)后 = exe 所在目录。
    不处理这个，打包后 config/output/data 会落到临时解压目录并随退出丢失。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


PROJECT_ROOT = _project_root()
CONFIG_PATH = PROJECT_ROOT / "config.json"

DEFAULT_KEYWORDS = {
    # 主题词：研究对象，命中 +3 分/词；require_topic_term=true 时必须命中至少一个
    # （示例方向：大语言模型与智能体——在 config.json 换成你自己的）
    "topic_terms": [
        "large language model", "llm", "language model", "foundation model",
    ],
    # 方法词：你的领域本体，命中 +2 分/词
    "method_terms": [
        "agent", "multi-agent", "tool use", "reasoning", "planning",
        "reflection",
    ],
    # 加分词：+1 分/词
    "bonus_terms": [
        "benchmark", "evaluation", "fine-tuning", "rlhf", "hallucination",
        "chain-of-thought", "memory", "multimodal",
    ],
    # 负向关键词：命中即整篇丢弃（示例：不想看综述/评述）
    "exclude_terms": ["survey", "review"],
}

DEFAULTS = {
    "keywords": DEFAULT_KEYWORDS,
    "days_lookback": 3,          # 期刊源回看天数（arXiv 量小，固定至少 7 天）
    "top_n": 12,                 # 早报最多收录篇数
    "min_score": 5,              # topic(3) + method(2) = 5，即主题词与方法词都命中
    "require_topic_term": True,
    "require_method_term": False,  # 设 true 则强制命中方法词
    # 期刊分级：journal_tiers 可覆盖/新增 journals.py 的默认映射；
    # min_journal_tier: 0=不过滤 / 1=只要顶刊 / 2=顶刊+权威 / 3=不过滤
    "journal_tiers": {},
    "min_journal_tier": 0,
    # 期刊分区接口（EasyScholar 免费 / OneScholar 会员）：填 Key 后日报自动标注
    # 中科院分区、JCR 分区、影响因子、Top；provider = easyscholar | onescholar | off
    "journal_rank": {
        "provider": "easyscholar",
        "easyscholar_key": "",
        "onescholar_key": "",
        "cache_days": 30,
    },
    "sources": {"arxiv": True, "openalex": True, "crossref": True,
                "scopus": True},
    # OpenAlex 免费额度按 IP 共享，容易被耗尽；填邮箱进礼貌池、填 key 最稳
    "openalex_email": "",
    "openalex_api_key": "",
    "crossref_email": "",
    # Elsevier（Scopus 检索 + 摘要回填）：https://dev.elsevier.com 免费申请
    "elsevier_api_key": "",
    # 摘要翻译：engine = google|mymemory|deepl|baidu|youdao|llm（自动回退其余可用引擎）
    "translate": {
        "engine": "llm",
        "deepl_api_key": "",
        "baidu_appid": "",
        "baidu_key": "",
        "youdao_appkey": "",
        "youdao_secret": "",
        "mymemory_email": "",
    },
    # 领域定制（开源复用：任何专业的研究生只改这两处 + keywords 即可换方向）
    "research_focus": "大语言模型与智能体（LLM Agents）——示例方向，请在 config.json 改成你自己的",
    "app_tagline": "论文日报 · 每天只推你方向的新文献",
    # LLM：服务商清单制（预设见 llm_providers.PROVIDER_PRESETS）。
    # providers 默认留空——只存用户实际填的差量（key/model/自定义端点），
    # 空 providers + 旧扁平字段会自动折进 custom 服务商（平滑迁移）。
    "llm": {
        "active": "",
        "system_prompt": "",
        "providers": {},
    },
    # 微信推送：provider = serverchan | pushplus | none
    "push": {
        "provider": "serverchan",
        "serverchan_sendkey": "",
        "pushplus_token": "",
        "enabled": True,
    },
    "output_dir": "output",
    "data_dir": "data",
    "language": "zh",
}


@dataclass
class Config:
    raw: dict = field(default_factory=lambda: copy.deepcopy(DEFAULTS))

    # --- 便捷访问 ---
    @property
    def keywords(self) -> dict:
        return self.raw["keywords"]

    @property
    def llm(self) -> dict:
        return self.raw["llm"]

    @property
    def push(self) -> dict:
        return self.raw["push"]

    def path(self, key: str) -> Path:
        return (PROJECT_ROOT / self.raw[key]).resolve()

    # --- 序列化 ---
    def to_json(self) -> str:
        return json.dumps(self.raw, ensure_ascii=False, indent=2)


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


_CONFIG_WRITE_LOCK = threading.Lock()


def update_config_file(path: Path, mutate) -> None:
    """原子地修改 config.json：读 → mutate(cfg dict) → 临时文件替换。
    进程内加锁——GUI 保存与后台线程同时写时避免读-改-写丢更新。"""
    with _CONFIG_WRITE_LOCK:
        cfg: dict = {}
        if path.exists():
            cfg = json.loads(path.read_text(encoding="utf-8"))
        mutate(cfg)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)


def apply_env(raw: dict) -> dict:
    """CI/Secrets 场景：环境变量覆盖配置（LITZB_ 前缀）。"""
    import os

    int_keys = {"LITZB_TOP_N", "LITZB_DAYS_LOOKBACK"}
    bool_keys = {"LITZB_PUSH_ENABLED"}
    overrides = {
        "LITZB_SERVERCHAN_SENDKEY": ("push", "serverchan_sendkey"),
        "LITZB_PUSHPLUS_TOKEN": ("push", "pushplus_token"),
        "LITZB_PUSH_PROVIDER": ("push", "provider"),
        "LITZB_PUSH_ENABLED": ("push", "enabled"),
        "LITZB_OPENALEX_API_KEY": ("openalex_api_key",),
        "LITZB_OPENALEX_EMAIL": ("openalex_email",),
        "LITZB_CROSSREF_EMAIL": ("crossref_email",),
        "LITZB_ELSEVIER_API_KEY": ("elsevier_api_key",),
        "LITZB_RANK_PROVIDER": ("journal_rank", "provider"),
        "LITZB_EASYSCHOLAR_KEY": ("journal_rank", "easyscholar_key"),
        "LITZB_ONESCHOLAR_KEY": ("journal_rank", "onescholar_key"),
        "LITZB_TRANSLATE_ENGINE": ("translate", "engine"),
        "LITZB_DEEPL_API_KEY": ("translate", "deepl_api_key"),
        "LITZB_BAIDU_APPID": ("translate", "baidu_appid"),
        "LITZB_BAIDU_KEY": ("translate", "baidu_key"),
        "LITZB_YOUDAO_APPKEY": ("translate", "youdao_appkey"),
        "LITZB_YOUDAO_SECRET": ("translate", "youdao_secret"),
        "LITZB_TOP_N": ("top_n",),
        "LITZB_DAYS_LOOKBACK": ("days_lookback",),
    }
    for env_key, path in overrides.items():
        val = os.environ.get(env_key)
        if not val:
            continue
        if env_key in int_keys:
            try:
                val = int(val)
            except ValueError:
                print(f"[config] 忽略非法整数 {env_key}={val!r}")
                continue
        elif env_key in bool_keys:
            val = val.strip().lower() in ("1", "true", "yes", "on")
        node = raw
        for key in path[:-1]:
            node = node.setdefault(key, {})
        node[path[-1]] = val

    # LLM 专用：写入 active 服务商（resolve 时会与预设合并）
    llm = raw.setdefault("llm", {})
    active_env = os.environ.get("LITZB_LLM_ACTIVE", "").strip()
    if active_env:
        llm["active"] = active_env
    target = str(llm.get("active", "") or "").strip() or "zhipu"
    for env_key, field in (("LITZB_LLM_API_KEY", "api_key"),
                           ("LITZB_LLM_BASE_URL", "base_url"),
                           ("LITZB_LLM_MODEL", "model")):
        val = os.environ.get(env_key, "").strip()
        if val:
            llm.setdefault("providers", {}).setdefault(target, {})[field] = val
    return raw


# 旧版配置键名（拼接拼写，公开仓库里不出现旧领域词字样；仅用于旧 config.json 兼容）
_LEGACY_TOPIC_GROUP = "bat" + "tery" + "_terms"
_LEGACY_REQUIRE_KEY = "require_" + "bat" + "tery" + "_term"


def normalize_legacy_keys(user: dict) -> dict:
    """旧版配置键名归一化（用户零迁移）：旧主题词分组 → topic_terms、
    旧必命中开关 → require_topic_term。新名已存在时以新名为准，旧名丢弃。"""
    kw = user.get("keywords")
    if isinstance(kw, dict):
        if _LEGACY_TOPIC_GROUP in kw and "topic_terms" not in kw:
            kw["topic_terms"] = kw[_LEGACY_TOPIC_GROUP]
        kw.pop(_LEGACY_TOPIC_GROUP, None)
    if _LEGACY_REQUIRE_KEY in user:
        if "require_topic_term" not in user:
            user["require_topic_term"] = user[_LEGACY_REQUIRE_KEY]
        user.pop(_LEGACY_REQUIRE_KEY, None)
    return user


def require_topic_value(raw: dict, default: bool = True) -> bool:
    """读取「主题词必须命中」开关：新名 require_topic_term 优先，旧名兜底。"""
    if "require_topic_term" in raw:
        return bool(raw["require_topic_term"])
    return bool(raw.get(_LEGACY_REQUIRE_KEY, default))


def load_config(path: Path | None = None) -> Config:
    path = path or CONFIG_PATH
    if not path.exists():
        return Config(raw=apply_env(copy.deepcopy(DEFAULTS)))  # 全默认值运行；CLI init 会落盘一份可编辑的
    try:
        user = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SystemExit(f"[config] {path} 不是合法 JSON: {e}")
    if not isinstance(user, dict):
        raise SystemExit(f"[config] {path} 顶层必须是对象 {{}}")
    return Config(raw=apply_env(deep_merge(DEFAULTS, normalize_legacy_keys(user))))
