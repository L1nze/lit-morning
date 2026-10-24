"""命令行入口：init / today / push-test / daemon / stats。"""
from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path

from . import __version__
from .config import CONFIG_PATH, PROJECT_ROOT, load_config
from .digest import run_digest
from .store import SeenStore


def _cmd_init(args) -> None:
    if CONFIG_PATH.exists() and not args.force:
        print(f"已存在 {CONFIG_PATH}（--force 可覆盖）")
        return
    example = PROJECT_ROOT / "config.example.json"
    cfg = load_config(example if example.exists() else None)
    CONFIG_PATH.write_text(cfg.to_json(), encoding="utf-8")
    print(f"已生成配置: {CONFIG_PATH}\n下一步: 填入 push.serverchan_sendkey（微信推送）"
          f" 和 llm.api_key（中文摘要，可选）")


def _cmd_today(args) -> None:
    cfg = load_config()
    result = run_digest(cfg, no_push=args.no_push, include_seen=args.include_seen,
                        limit=args.limit, days=args.days)
    if args.open and result.get("html"):
        webbrowser.open(Path(result["html"]).as_uri())


def _cmd_push_test(args) -> None:
    from .push import channel_name, configured_channels, push_one
    cfg = load_config()
    channels = configured_channels(cfg.push)
    if not channels:
        print("✘ 没有已配置的推送渠道（检查 push.provider/fallback_provider 与对应凭据）")
        sys.exit(1)
    failed = False
    for pid in channels:
        ok, msg = push_one(pid, cfg.push, "✅ lit-zaobao 测试消息",
                           "如果你看到这条消息，说明该推送通道已经打通。")
        print(("✔ " if ok else "✘ ") + f"[{channel_name(pid)}] {msg}")
        failed = failed or not ok
    if failed:
        sys.exit(1)


def _cmd_stats(args) -> None:
    cfg = load_config()
    store = SeenStore(cfg.path("data_dir") / "seen.db")
    rows = store.stats_by_day(days=args.days)
    store.close()
    if not rows:
        print("还没有记录，先跑一次 `python bot.py today --no-push`")
        return
    print(f"近 {args.days} 天收录统计（日期 / 来源 / 篇数）:")
    for day, source, n in rows:
        print(f"  {day}  {source:<10} {n}")


def _norm_ref(s: str) -> str:
    s = (s or "").strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if s.startswith(prefix):
            s = s[len(prefix):]
    return s


def _match_paper(query: str, rec: dict) -> bool:
    q = _norm_ref(query)
    if not q:
        return False
    for field in (rec.get("id", ""), rec.get("doi", ""), rec.get("url", "")):
        if q == _norm_ref(str(field)):
            return True
    return q in (rec.get("title", "") or "").lower()


def _cmd_like(args) -> None:
    import json as json_mod

    from .paper import Paper
    from .profile import save_like

    cfg = load_config()
    records: list[dict] = []
    for f in sorted(cfg.path("output_dir").glob("digest-*.json"), reverse=True):
        try:
            records.extend(json_mod.loads(f.read_text(encoding="utf-8")))
        except (json_mod.JSONDecodeError, OSError):
            continue

    for query in args.paper:
        rec = next((r for r in records if _match_paper(query, r)), None)
        if not rec:
            print(f"✘ 找不到 {query}（先跑 `python bot.py today --no-push` 再点赞）")
            continue
        fields = {k: rec.get(k, "") for k in
                  ("id", "source", "title", "authors", "abstract", "url",
                   "published", "doi", "journal")}
        n = save_like(cfg.path("data_dir"), Paper(**fields))
        print(f"❤ 已点赞 [{n}]: {rec.get('title', '')[:60]}")


def _cmd_likes(args) -> None:
    from .profile import load_likes
    cfg = load_config()
    likes = load_likes(cfg.path("data_dir"))
    if not likes:
        print("还没有点赞记录：`python bot.py like <论文id或链接>`")
        return
    for x in likes:
        print(f"  ❤ {x.get('liked_at', '')[:10]} [{x.get('id')}] {x.get('title', '')[:60]}")


def _cmd_unlike(args) -> None:
    from .profile import remove_like
    cfg = load_config()
    for ref in args.paper:
        if remove_like(cfg.path("data_dir"), ref):
            print(f"已取消点赞: {ref}")
        else:
            print(f"点赞列表里没找到: {ref}")


def _cmd_models(args) -> None:
    from . import llm_providers as lp

    cfg = load_config()

    if args.use:
        pid, _, model = args.use.partition("/")
        pid, model = pid.strip(), model.strip()
        providers = lp.all_providers(cfg.llm)
        if pid not in providers:
            print(f"✘ 未知服务商 {pid!r}，可选: {', '.join(providers)}")
            sys.exit(1)
        if model and providers[pid]["models"] and model not in providers[pid]["models"]:
            print(f"⚠ {model} 不在 {pid} 候选清单里，仍按你的输入写入")
        lp.write_active(CONFIG_PATH, pid, model)
        p = lp.all_providers(load_config().llm)[pid]
        print(f"✔ 已切换: {p['name']} · {p['model'] or '(未选模型)'}")
        return

    if args.test is not None:
        providers = lp.all_providers(cfg.llm)
        pid = args.test if isinstance(args.test, str) and args.test else lp.active_id(cfg.llm)
        if pid not in providers:
            print(f"✘ 未知服务商 {pid!r}"); sys.exit(1)
        resolved = dict(lp.resolve(cfg.llm))
        if pid != lp.active_id(cfg.llm):
            p = providers[pid]
            resolved.update(provider_id=pid, name=p["name"], base_url=p["base_url"],
                            api_key=p["api_key"], model=p["model"])
        print(f"测试 {resolved['name']} · {resolved['model']} ...")
        ok, msg = lp.test_connection(resolved)
        print(("✔ " if ok else "✘ ") + msg)
        if not ok:
            sys.exit(1)
        return

    # 默认：列出服务商清单
    providers = lp.all_providers(cfg.llm)
    active = lp.active_id(cfg.llm)
    marks = {"ready": "●", "partial": "◐", "empty": "○"}
    print("服务商清单（●可用 ◐缺项 ○未配置；* 当前生效）")
    print("切换: `python bot.py models --use <id>/<model>`  测试: `python bot.py models --test`\n")
    for pid, p in providers.items():
        mark = marks[lp.provider_status(p)]
        cur = "*" if pid == active else " "
        models = ", ".join(p["models"]) if p["models"] else "(无候选模型)"
        reason = ""
        if lp.provider_status(p) == "partial":
            reason = "  ← 未填 key" if p.get("key_required", True) and not p["api_key"] else "  ← 未选模型"
        elif lp.provider_status(p) == "empty":
            reason = "  ← 未填 base_url"
        print(f" {cur} {mark} {pid:<12} {p['name']:<10} {p['model'] or '-':<22} [{models}]{reason}")
    sp = (cfg.llm.get("system_prompt") or "").strip()
    print(f"\n系统提示词: {'config 自定义' if sp else '内置默认（llm.system_prompt 可覆盖）'}")


def _cmd_weekly(args) -> None:
    from .weekly import run_weekly
    cfg = load_config()
    run_weekly(cfg, days=args.days)


def _cmd_rank(args) -> None:
    from . import journal_rank

    cfg = load_config()
    if not args.journal:
        ok, msg = journal_rank.test_connection(cfg.raw)
        print(("✔ " if ok else "✘ ") + msg)
        sys.exit(0 if ok else 1)
    if not journal_rank.is_configured(cfg.raw):
        print("期刊分区未配置：在设置里选 EasyScholar（免费）或 OneScholar 并填 Key。")
        sys.exit(1)
    service = journal_rank.JournalRankService(cfg.raw, cfg.path("data_dir"))
    for name in args.journal:
        rank = service.lookup(name)
        if rank:
            print(f"  ✔ {name}: {journal_rank.summary(rank)}")
        else:
            err = service.error_for(name)
            print(f"  – {name}: 无分区数据" +
                  (f"（{err}）" if err else "（期刊名建议用官方英文全名）"))
    service.save()


def _cmd_serve(args) -> None:
    from .server import serve
    serve(port=args.port, open_browser=args.open)


def _cmd_app(args) -> None:
    from .server import serve
    serve(port=args.port, app_mode=True)


def _cmd_gui(args) -> None:
    from .gui import main as gui_main
    gui_main()


def _cmd_doctor(args) -> None:
    from .doctor import run_doctor
    from pathlib import Path as _P
    sys.exit(run_doctor(_P(args.config) if args.config else None))


def _cmd_daemon(args) -> None:
    from .daemon import run_daemon
    run_daemon(args.at, args.no_push)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bot.py",
        description="文献早报机器人：每天抓你研究方向的新文献，摘要后推送到微信")
    p.add_argument("--version", action="version", version=f"lit-zaobao {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init", help="生成 config.json 配置文件")
    sp.add_argument("--force", action="store_true", help="覆盖已有配置")
    sp.set_defaults(func=_cmd_init)

    sp = sub.add_parser("today", help="立刻生成今日早报")
    sp.add_argument("--no-push", action="store_true", help="只生成文件，不推送")
    sp.add_argument("--include-seen", action="store_true", help="包含已推送过的文献")
    sp.add_argument("--limit", type=int, default=None, help="最多收录篇数")
    sp.add_argument("--days", type=int, default=None, help="回看天数")
    sp.add_argument("--open", action="store_true", help="生成后自动在浏览器打开 HTML")
    sp.set_defaults(func=_cmd_today)

    sp = sub.add_parser("push-test", help="发一条测试消息到微信")
    sp.set_defaults(func=_cmd_push_test)

    sp = sub.add_parser("stats", help="查看收录统计")
    sp.add_argument("--days", type=int, default=14)
    sp.set_defaults(func=_cmd_stats)

    sp = sub.add_parser("models", help="查看/切换 LLM 服务商与模型")
    sp.add_argument("--use", metavar="PROVIDER[/MODEL]", help="切换生效服务商（可同时指定模型）")
    sp.add_argument("--test", nargs="?", const="", default=None, metavar="PROVIDER",
                    help="测试连接（默认测当前生效服务商）")
    sp.set_defaults(func=_cmd_models)

    sp = sub.add_parser("weekly", help="生成周报：趋势/来源/期刊层次/热词")
    sp.add_argument("--days", type=int, default=7)
    sp.set_defaults(func=_cmd_weekly)

    sp = sub.add_parser("rank", help="查期刊分区（中科院/JCR/影响因子）")
    sp.add_argument("journal", nargs="*", help="期刊英文名；不填则测试当前分区服务连接")
    sp.set_defaults(func=_cmd_rank)

    sp = sub.add_parser("like", help="给某篇论文点赞（影响后续排序加权）")
    sp.add_argument("paper", nargs="+", help="论文 id / 链接 / 标题片段")
    sp.set_defaults(func=_cmd_like)

    sp = sub.add_parser("likes", help="查看点赞列表")
    sp.set_defaults(func=_cmd_likes)

    sp = sub.add_parser("unlike", help="取消点赞")
    sp.add_argument("paper", nargs="+", help="论文 id / 链接 / 标题片段")
    sp.set_defaults(func=_cmd_unlike)

    sp = sub.add_parser("serve", help="启动本地网页控制台（127.0.0.1）")
    sp.add_argument("--port", type=int, default=8000)
    sp.add_argument("--open", action="store_true", help="自动在浏览器打开")
    sp.set_defaults(func=_cmd_serve)

    sp = sub.add_parser("app", help="应用模式：自动选端口 + Edge 应用窗口（exe 的入口）")
    sp.add_argument("--port", type=int, default=8000)
    sp.set_defaults(func=_cmd_app)

    sp = sub.add_parser("gui", help="原生桌面窗口（tkinter，exe 的默认入口）")
    sp.set_defaults(func=_cmd_gui)

    sp = sub.add_parser("doctor", help="全链路自检（真实网络逐环节检查）")
    sp.add_argument("--config", default=None, help="指定 config.json 路径")
    sp.set_defaults(func=_cmd_doctor)

    sp = sub.add_parser("daemon", help="常驻运行，每天定时生成并推送")
    sp.add_argument("--at", default="08:30", help="每天执行时间，默认 08:30")
    sp.add_argument("--no-push", action="store_true")
    sp.set_defaults(func=_cmd_daemon)
    return p


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
