"""全链路自检：`python bot.py doctor [--config 路径]`

逐环节活体检查（真实网络，非 mock）：
配置文件 → 目录可写 → 五个数据源 → LLM 连接 → 翻译引擎 → 推送通道 → 已读库 → GUI 锁。
每项输出 状态(ok/warn/skip/fail) + 细节；有 fail 退出码 1。供日常排障与 CI 冒烟复用。
"""
from __future__ import annotations

import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

from .config import load_config
from .llm_providers import provider_status, resolve, test_connection
from .push import channel_name, configured_channels, push_one
from .push import smtp as push_smtp
from .sources import (arxiv_source, crossref_source, elsevier_source,
                      openalex_source)
from .store import SeenStore
from .translate import ENGINES, translate_text

_ok, _warn, _skip, _fail = "ok", "warn", "skip", "fail"
_ICON = {"ok": "✔", "warn": "⚠", "skip": "–", "fail": "✘"}


def _check(name: str, status: str, detail: str, rows: list) -> None:
    rows.append((name, status, detail))


def run_doctor(config_path: Path | None = None) -> int:
    rows: list[tuple[str, str, str]] = []
    cfg = load_config(config_path)
    actual_path = config_path or "(项目根 config.json)"

    # 1. 配置与目录
    _check("配置加载", _ok, f"{actual_path} 解析成功（关键词 {len(cfg.keywords['method_terms'])} 方法词）", rows)
    for label, key in (("输出目录", "output_dir"), ("数据目录", "data_dir")):
        d = cfg.path(key)
        try:
            d.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=d, delete=True):
                pass
            _check(f"{label}可写", _ok, str(d), rows)
        except OSError as e:
            _check(f"{label}可写", _fail, str(e), rows)

    # 2. 数据源（真实抓取，3 天窗口）
    cutoff = date.today() - timedelta(days=3)
    arxiv_cutoff = date.today() - timedelta(days=7)
    kw = cfg.keywords
    src_jobs = [
        ("arXiv", lambda: arxiv_source.fetch(arxiv_cutoff, kw)),
        ("OpenAlex", lambda: openalex_source.fetch(
            cutoff, kw, email=cfg.raw.get("openalex_email", ""),
            api_key=cfg.raw.get("openalex_api_key", ""))),
        ("Crossref", lambda: crossref_source.fetch(cutoff, kw, email=cfg.raw.get("crossref_email", ""))),
    ]
    for name, fn in src_jobs:
        try:
            got = fn()
            status = _ok if got else _warn
            _check(f"数据源 {name}", status, f"{len(got)} 条（近窗口）" if got else "0 条（窗口内无新文或受限）", rows)
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            msg = msg.rsplit("::", 1)[-1].strip() if "::" in msg else msg
            degraded = "429" in msg or "Too Many" in msg
            _check(f"数据源 {name}", _warn if degraded else _fail, msg[:90], rows)

    # Scopus（key 门控）
    if cfg.raw.get("elsevier_api_key"):
        try:
            got = elsevier_source.fetch(cutoff, kw, api_key=cfg.raw["elsevier_api_key"])
            _check("数据源 Scopus", _ok, f"{len(got)} 条", rows)
        except Exception as e:  # noqa: BLE001
            _check("数据源 Scopus", _fail, str(e)[:90], rows)
    else:
        _check("数据源 Scopus", _skip, "未配置 elsevier_api_key（dev.elsevier.com 免费申请）", rows)

    # 期刊分区（Key 门控）
    from . import journal_rank
    if journal_rank.is_configured(cfg.raw):
        rank_name = journal_rank.provider_name(journal_rank.settings(cfg.raw)["provider"])
        ok, msg = journal_rank.test_connection(cfg.raw)
        _check(f"期刊分区（{rank_name}）", _ok if ok else _fail, msg[:90], rows)
    else:
        _check("期刊分区", _skip,
               "未配置 Key（设置里选 EasyScholar/OneScholar 后日报自动标注分区）", rows)

    # 3. LLM
    r = resolve(cfg.llm)
    st = provider_status({**r, "key_required": True})
    if r.get("api_key"):
        ok, msg = test_connection(r)
        _check(f"LLM（{r['name']} · {r['model']}）", _ok if ok else _fail, msg[:90], rows)
    else:
        _check(f"LLM（{r['name']}）", _skip, f"状态 {st}：未填 key，摘要将用规则降级", rows)

    # 4. 翻译
    sample = "Large language model agents improve multi-step reasoning and evaluation."
    try:
        zh, engine = translate_text(sample, cfg.raw.get("translate", {}), cfg.llm)
        _check("翻译引擎", _ok, f"[{ENGINES[engine]['name']}] {zh[:40]}", rows)
    except Exception as e:  # noqa: BLE001
        _check("翻译引擎", _warn, str(e)[:100], rows)

    # 5. 推送（主+备逐通道；网络渠道真发一条自检，smtp 只做凭据格式校验不真发信）
    channels = configured_channels(cfg.push)
    if channels:
        for pid in channels:
            if pid == "smtp":
                ok, msg = push_smtp.validate(cfg.push)
            else:
                ok, msg = push_one(pid, cfg.push, "✅ lit-zaobao doctor 自检", "doctor 自检消息。")
            _check(f"推送（{channel_name(pid)}）", _ok if ok else _fail, msg[:90], rows)
    else:
        _check("推送", _skip, f"provider={cfg.push.get('provider')} 未填凭据", rows)

    # 6. 已读库与锁
    try:
        with SeenStore(cfg.path("data_dir") / "seen.db") as store:
            n = len(store.known_keys())
        _check("已读库", _ok, f"{n} 条已读记录", rows)
    except Exception as e:  # noqa: BLE001
        _check("已读库", _fail, str(e)[:90], rows)

    from .gui import _acquire_lock
    lock = _acquire_lock()
    if lock:
        _check("GUI 单实例锁", _ok, f"端口 18731 可绑定", rows)
        lock.close()
    else:
        _check("GUI 单实例锁", _warn, "端口被占用（GUI 可能正在运行）", rows)

    # 报告
    width = max(len(r[0]) for r in rows)
    print(f"\nlit-zaobao doctor · {date.today()} · 配置: {actual_path}\n")
    order = {_ok: 0, _skip: 1, _warn: 2, _fail: 3}
    worst_status = _ok
    for name, status, detail in rows:
        print(f"  {_ICON[status]} {name:<{width}}  {detail}")
        if order[status] > order[worst_status]:
            worst_status = status
    summary = {_ok: "全部正常", _skip: "全部正常（部分可选项未配置）",
               _warn: "有降级项（见 ⚠，不影响主流程）", _fail: "存在失败项（见 ✘，需处理）"}
    print(f"\n结论: {summary[worst_status]}")
    return 1 if worst_status == _fail else 0


def main(argv: list[str] | None = None) -> None:
    path = None
    args = argv if argv is not None else sys.argv[1:]
    if "--config" in args:
        i = args.index("--config")
        path = Path(args[i + 1])
    sys.exit(run_doctor(path))
