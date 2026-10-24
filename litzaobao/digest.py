"""早报生成管线：抓取 → 打分去重 → 期刊分级 → 个性化 → 截断 TopN
→ 摘要补全 → LLM 摘要 → 渲染落盘 → 推送（成功才记已读）。"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from . import enrich as enrich_mod
from . import filtering, journal_rank, render, summarize
from .config import Config, require_topic_value
from .journals import SCORE_BONUS, journal_tier
from .paper import Paper
from .profile import build_profile, boost_paper, load_likes
from .push import push_configured, push_wechat
from .sources import arxiv_source, crossref_source, elsevier_source, openalex_source
from .store import SeenStore


def _fetch_all(cfg: Config, cutoff: date, arxiv_cutoff: date) -> tuple[list[Paper], list[str]]:
    papers: list[Paper] = []
    errors: list[str] = []
    src = cfg.raw["sources"]
    jobs: list[tuple[str, object]] = []
    if src.get("arxiv"):
        jobs.append(("arXiv", lambda: arxiv_source.fetch(arxiv_cutoff, cfg.keywords)))
    if src.get("openalex"):
        jobs.append(("OpenAlex", lambda: openalex_source.fetch(
            cutoff, cfg.keywords,
            email=cfg.raw.get("openalex_email", ""),
            api_key=cfg.raw.get("openalex_api_key", ""))))
    if src.get("crossref"):
        jobs.append(("Crossref", lambda: crossref_source.fetch(
            cutoff, cfg.keywords, email=cfg.raw.get("crossref_email", ""))))
    if src.get("scopus", True) and cfg.raw.get("elsevier_api_key"):
        jobs.append(("Scopus", lambda: elsevier_source.fetch(
            cutoff, cfg.keywords, api_key=cfg.raw["elsevier_api_key"])))

    # 并发抓取：线程里只跑 fetch（纯网络 IO），print 与汇总在主线程按 jobs 定义顺序进行，
    # 保证输出顺序稳定、错误信息格式与串行版一致。
    results: list[tuple[list[Paper] | None, str]] = []   # 与 jobs 一一对应：(结果, 错误信息)
    with ThreadPoolExecutor(max_workers=min(6, len(jobs)) or 1) as pool:
        futures = [pool.submit(fn) for _, fn in jobs]
        for fut in futures:
            try:
                results.append((fut.result(), ""))
            except Exception as e:  # noqa: BLE001 - 单源失败不影响整体
                results.append((None, str(e)))

    for (name, _), (got, err) in zip(jobs, results):
        if got is not None:
            papers.extend(got)
            print(f"  [抓取] {name}: {len(got)} 条")
        else:
            msg = err
            if "::" in msg:  # 去掉 URL 细节，报错保持可读
                msg = msg.rsplit("::", 1)[-1].strip()
            errors.append(f"{name} 不可用({msg[:100]})")
            print(f"  [抓取] {name}: 失败 - {msg[:100]}")
    return papers, errors


def run_digest(cfg: Config, *, no_push: bool = False, include_seen: bool = False,
               limit: int | None = None, days: int | None = None) -> dict:
    """跑一轮完整管线，返回结果摘要（供 CLI / daemon / 测试共用）。"""
    today = date.today()
    days = days or cfg.raw["days_lookback"]
    cutoff = today - timedelta(days=days)
    arxiv_cutoff = min(cutoff, today - timedelta(days=7))  # arXiv 量小，至少回看 7 天

    print(f"[1/6] 抓取近 {days} 天文献 (cutoff={cutoff}) ...")
    papers, source_errors = _fetch_all(cfg, cutoff, arxiv_cutoff)

    print(f"[2/6] 关键词打分：{len(papers)} 条候选 ...")
    kw = cfg.keywords
    for p in papers:
        filtering.score_paper(p, kw,
                              require_topic=require_topic_value(cfg.raw),
                              require_method=cfg.raw.get("require_method_term", False),
                              min_score=cfg.raw.get("min_score", 5))
    rejected = [p for p in papers if p.score == 0 and p.matched]   # 差一点过线（排除词/缺必备词的 matched 为空，不会被捞回）
    papers = [p for p in papers if p.score > 0]
    papers = filtering.dedup_papers(papers)
    print(f"  过滤+去重后 {len(papers)} 条")

    with SeenStore(cfg.path("data_dir") / "seen.db") as store:  # 异常也保证关闭
        if not include_seen:
            papers = store.filter_new(papers)
            print(f"  去掉已推送后 {len(papers)} 条")

        # 期刊分级：赋 tier → 可选门槛过滤 → 排序加分
        tier_overrides = cfg.raw.get("journal_tiers") or {}
        min_tier = int(cfg.raw.get("min_journal_tier", 0) or 0)
        kept = []
        for p in papers:
            p.journal_tier = journal_tier(p.journal, tier_overrides)
            if min_tier and p.source != "arxiv" and p.journal_tier > min_tier:
                continue  # arXiv 预印本不受期刊门槛限制
            p.score += SCORE_BONUS.get(p.journal_tier, 0)
            kept.append(p)
        if min_tier:
            print(f"  期刊门槛 ≤ tier{min_tier}：{len(kept)}/{len(papers)} 条保留")
        papers = kept

        # 个性化加权：与点赞画像重合的论文加分
        likes = load_likes(cfg.path("data_dir"))
        if likes:
            profile = build_profile(likes, kw)
            boosted = sum(1 for p in papers if boost_paper(p, profile))
            print(f"  兴趣画像(点赞 {len(likes)} 篇): {boosted} 条获得加分")

        # 语义增强（可选模块，懒加载）：embedding 按点赞/方向画像给论文加分、
        # 捞回 borderline 论文。semantic.enabled 可整体关闭；
        # 模块不存在或内部出错都不影响早报主流程。
        sem_cfg = cfg.raw.get("semantic") or {}
        if sem_cfg.get("enabled", True):
            try:
                from . import semantic
                sem_llm = dict(cfg.llm, semantic_rescue_top=sem_cfg.get("rescue_top", 2))
                papers, sem_note = semantic.augment(
                    papers, rejected, str(cfg.raw.get("research_focus", "")),
                    sem_llm, cfg.path("data_dir"))
                print(f"  语义增强: {sem_note or '未启用'}")
            except Exception as e:  # noqa: BLE001 - 语义增强失败不影响早报
                print(f"  语义增强: 跳过（{e}）")
        else:
            print("  语义增强: 已关闭（semantic.enabled=false）")

        top_n = limit or cfg.raw["top_n"]
        papers = sorted(papers, key=lambda p: (p.score, p.published), reverse=True)[:top_n]

        if not papers:
            msg = "今天没有新的相关文献，跳过摘要与推送。"
            print(msg)
            return {"count": 0, "pushed": False, "message": msg, "errors": source_errors}

        # 期刊分区（中科院/JCR/IF/Top）：只查最终这批，结果随日报展示。
        # 失败只降级为「无分区数据」，不影响后面的摘要与推送。
        try:
            rank_stats = journal_rank.annotate_papers(papers, cfg.raw, cfg.path("data_dir"))
            if rank_stats.get("enabled"):
                line = (f"  期刊分区[{journal_rank.provider_name(rank_stats['provider'])}]: "
                        f"{rank_stats['hit']}/{rank_stats['total']} 篇有数据"
                        f"（查询 {rank_stats['requests']} 次 · 缓存命中 {rank_stats['cached']}）")
                print(line)
                if rank_stats.get("error"):
                    print(f"    ⚠ {rank_stats['error']} 个期刊查询失败："
                          f"{rank_stats.get('last_error', '')[:80]}")
            else:
                print("  期刊分区: 未配置（设置里填 EasyScholar 或 OneScholar 的 Key 后自动标注）")
        except Exception as e:  # noqa: BLE001 - 分区接口异常不阻断早报
            print(f"  期刊分区: 跳过（{e}）")

        filled = enrich_mod.enrich_abstracts(
            papers, email=cfg.raw.get("openalex_email", ""),
            api_key=cfg.raw.get("openalex_api_key", ""),
            elsevier_key=cfg.raw.get("elsevier_api_key", ""))
        if filled:
            print(f"  摘要补全: {filled} 条 (via OpenAlex)")

        print(f"[3/6] LLM 摘要 {len(papers)} 篇 ...")
        from .llm_providers import resolve as resolve_llm
        llm_resolved = resolve_llm(cfg.llm)
        if llm_resolved.get("api_key"):
            print(f"  服务商: {llm_resolved['name']} · {llm_resolved['model']}")
        overview = summarize.enrich(papers, cfg.llm, cfg.raw.get("research_focus", ""),
                                    cache_path=cfg.path("data_dir") / "summary_cache.json")

        print("[4/6] 渲染 Markdown / HTML ...")
        tagline = cfg.raw.get("app_tagline", "") or render.DEFAULT_TAGLINE
        md = render.render_markdown(papers, overview, today, source_errors, tagline=tagline)
        html = render.render_html(papers, overview, today, source_errors, tagline=tagline)
        out_dir = cfg.path("output_dir")
        out_dir.mkdir(parents=True, exist_ok=True)
        md_path = out_dir / f"digest-{today.isoformat()}.md"
        html_path = out_dir / f"digest-{today.isoformat()}.html"
        json_path = out_dir / f"digest-{today.isoformat()}.json"
        md_path.write_text(md, encoding="utf-8")
        html_path.write_text(html, encoding="utf-8")
        json_path.write_text(
            json.dumps([p.to_dict() for p in papers], ensure_ascii=False, indent=2),
            encoding="utf-8")

        # 归档索引 / RSS（可选模块，懒加载）：把历史日报汇总为 index.html + RSS；
        # 模块不存在或出错只跳过，不影响早报产出。
        site_url = str(cfg.raw.get("site_url", "") or "").strip()
        try:
            from . import archive
            idx = archive.write_index(out_dir, site_url)
            feed = archive.write_rss(out_dir, site_url)
            if idx:
                print(f"  归档索引: {idx.name}" + (f" · RSS: {feed.name}" if feed else ""))
        except Exception as e:  # noqa: BLE001
            print(f"  归档索引/RSS: 跳过（{e}）")

        print("[5/6] 推送到微信 ...")
        pushed, push_msg = False, "已跳过(--no-push)"
        if not no_push and cfg.push.get("enabled", True):
            title = f"📚 文献早报 {today.isoformat()} · {len(papers)} 篇"
            full_url = f"{site_url.rstrip('/')}/digest-{today.isoformat()}.html" if site_url else ""
            pushed, push_msg = push_wechat(cfg.push, title, md, full_url=full_url)
            print(f"  {'✔' if pushed else '✘'} {push_msg}")
        else:
            print(f"  {push_msg}")

        print("[6/6] 记录已推送 ...")
        # 记已读条件：推成功 / 明确跳过 / 推送关闭 / 凭据未配置（视同本地模式）。
        # 凭据已配置但网络失败 → 不记，明天自动重试同一批。
        if pushed or no_push or not cfg.push.get("enabled", True) or not push_configured(cfg.push):
            store.mark_seen(papers, today)
        else:
            print("  ⚠ 推送失败，本批文献明天重试（不记已读）")

    print(f"\n完成：{len(papers)} 篇 → {html_path}")
    for i, p in enumerate(papers, 1):
        print(f"  {i:>2}. [{p.score}] {p.title[:70]}")
    return {"count": len(papers), "pushed": pushed, "message": push_msg,
            "errors": source_errors, "html": str(html_path), "papers": papers}
