"""tkinter 原生 GUI：`python bot.py gui`（也是 exe 的默认入口）。

纯标准库，零第三方依赖。主窗左区为双页签：
- 「论文阅读」：点选任何论文（最新候选 / 我的点赞 / 历史日报加载的批次）即显示
  完整信息——标题、期刊分级徽章、⚡亮点、中文三句话摘要、英文摘要全文、原文链接；
- 「运行日志」：生成早报时的滚动输出（运行时自动切过去）。

设置框（LitBoard 式）：LLM 服务商 + Base URL + API Key（留空保留）+ 模型 +
测试连接，系统提示词，微信推送，数据源 API（OpenAlex / Elsevier Key 与邮箱）。

线程模型：运行管线与网络测试放后台线程，日志经 queue 回主线程渲染，UI 不冻结。
单实例：绑定本地锁端口，重复启动提示后退出。
"""
from __future__ import annotations

import json
import queue
import socket
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import messagebox, ttk

from . import autorun, journal_rank
from .config import CONFIG_PATH, Config, load_config, update_config_file
from .digest import run_digest
from .journals import badge as tier_badge
from .llm_providers import (active_id, all_providers, apply_llm_save, mask_key,
                            provider_status, resolve, test_connection)
from .paper import Paper
from .profile import load_likes, remove_like, save_like
from .push import apply_push_save, push_configured, push_wechat
from .summarize import (default_system_prompt, effective_system_prompt,
                        normalize_custom_prompt)
from .translate import ENGINES, cache_lookup, cached_translate, translate_text

_LOCK_PORT = 18731  # 单实例锁

# 翻译引擎 → 需要的凭据字段（其余隐藏，设置框不再一屏框）
TRANS_FIELDS: dict[str, list[tuple[str, str, bool]]] = {
    "llm": [],
    "volc": [],
    "tencent": [],
    "google": [],
    "mymemory": [("mymemory_email", "MyMemory 邮箱(提额)", False)],
    "deepl": [("deepl_api_key", "DeepL Key", True)],
    "baidu": [("baidu_appid", "百度 AppID", False), ("baidu_key", "百度 Key", True)],
    "youdao": [("youdao_appkey", "有道 AppKey", False), ("youdao_secret", "有道 Secret", True)],
}


# ------------------------------------------------------------------ 纯函数（可测）
def format_paper_detail(rec: dict, liked: bool = False, translation_zh: str = "") -> str:
    """论文阅读页的纯文本排版（与 UI 解耦，便于单测）。
    translation_zh 非空时在中文摘要之后、英文摘要之前插入「中文译文」段。"""
    src_label = {"arxiv": "arXiv 预印本", "openalex": "OpenAlex",
                 "crossref": "期刊", "scopus": "期刊"}
    lines = [rec.get("title", ""), ""]
    meta = [src_label.get(rec.get("source", ""), rec.get("source", ""))]
    if rec.get("journal"):
        meta.append(str(rec["journal"])[:48])
    if rec.get("published"):
        meta.append(str(rec["published"]))
    badge = tier_badge(int(rec.get("journal_tier", 3) or 3))
    if badge:
        meta.insert(0, badge)
    if rec.get("score"):
        meta.append(f"匹配分 {rec['score']}")
    authors = rec.get("authors") or []
    if authors:
        meta.append(str(authors[0]) + (f" 等 {len(authors)} 人" if len(authors) > 1 else ""))
    lines.append(" · ".join(meta))
    rank_text = journal_rank.summary(rec.get("journal_rank"))
    if rank_text:
        lines.append("📊 " + rank_text)
    if liked:
        lines.append("❤ 已点赞")
    if rec.get("matched"):
        lines.append("命中: " + ", ".join(rec["matched"][:8]))
    lines.append("")
    if rec.get("highlight"):
        lines.append("⚡ " + str(rec["highlight"]))
        lines.append("")
    if rec.get("summary_zh"):
        lines.append(str(rec["summary_zh"]))
        lines.append("")
    if translation_zh:
        lines.append("— 中文译文 —")
        lines.append(str(translation_zh))
        lines.append("")
    if rec.get("abstract"):
        lines.append("— 英文摘要 —")
        lines.append(str(rec["abstract"]))
        lines.append("")
    if rec.get("url"):
        lines.append("原文: " + str(rec["url"]))
    return "\n".join(lines).strip()


class UiQueue:
    """线程安全的 UI 任务队列：工作线程 put()，主循环 drain() 后在 UI 线程执行。
    取代跨线程 widget.after() ——后者是 tkinter 官方不保证线程安全的用法，
    实测会触发 Tcl_AsyncDelete 崩溃（exe 闪退的元凶之一）。"""

    def __init__(self):
        import queue as _q
        self._q: "_q.Queue" = _q.Queue()

    def put(self, fn) -> None:
        self._q.put(fn)

    def drain(self) -> list:
        """取空队列（回调都很轻，一次 tick 清完，避免积压上限）。"""
        out = []
        while True:
            try:
                out.append(self._q.get_nowait())
            except Exception:  # noqa: BLE001 - queue.Empty
                return out


class LogWriter:
    def __init__(self, q: "queue.Queue"):
        self.q = q

    def write(self, s: str) -> None:
        if s:
            self.q.put(s)

    def flush(self) -> None:
        pass


# ------------------------------------------------------------------ 设置框
class SettingsDialog(tk.Toplevel):
    def __init__(self, app: "App"):
        super().__init__(app)
        self.app = app
        self.title("设置")
        self.geometry("700x780")
        self.minsize(560, 480)
        self.resizable(True, True)
        self.transient(app)
        self.grab_set()
        self.cfg_path = app.config_path
        self._build()
        self.reload()
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _close(self) -> None:
        self.unbind_all("<MouseWheel>")
        self.destroy()

    # ---- 构造（Canvas+滚动条包裹，内容再高也不怕） ----
    def _build(self) -> None:
        pad = {"padx": 10, "pady": 4}
        canvas = tk.Canvas(self, highlightthickness=0)
        vsb = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        frm = ttk.Frame(canvas)
        win = canvas.create_window((0, 0), window=frm, anchor="nw")
        frm.bind("<Configure>",
                 lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(win, width=e.width))
        self.bind_all("<MouseWheel>",
                      lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))

        # LLM 服务商
        ttk.Label(frm, text="LLM 服务商", font=("", 10, "bold")).grid(row=0, column=0, sticky="w", **pad)
        self.provider_var = tk.StringVar()
        self.provider_cb = ttk.Combobox(frm, textvariable=self.provider_var,
                                        state="readonly", width=34)
        self.provider_cb.grid(row=1, column=0, sticky="w", **pad)
        self.provider_cb.bind("<<ComboboxSelected>>", lambda _e: self._fill_provider())
        self.active_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(frm, text="设为当前生效", variable=self.active_var).grid(row=1, column=1, sticky="w", **pad)

        ttk.Label(frm, text="Base URL").grid(row=2, column=0, sticky="w", **pad)
        self.baseurl_var = tk.StringVar()
        ttk.Entry(frm, textvariable=self.baseurl_var, width=52).grid(row=3, column=0, columnspan=2, sticky="w", **pad)

        ttk.Label(frm, text="API Key").grid(row=4, column=0, sticky="w", **pad)
        self.key_var = tk.StringVar()
        ttk.Entry(frm, textvariable=self.key_var, width=40, show="*").grid(row=5, column=0, sticky="w", **pad)
        self.clear_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm, text="清除", variable=self.clear_var).grid(row=5, column=1, sticky="w", **pad)
        self.key_hint = ttk.Label(frm, text="", foreground="#6a737d")
        self.key_hint.grid(row=6, column=0, columnspan=2, sticky="w", **pad)

        ttk.Label(frm, text="模型").grid(row=7, column=0, sticky="w", **pad)
        self.model_var = tk.StringVar()
        self.model_cb = ttk.Combobox(frm, textvariable=self.model_var, width=34)
        self.model_cb.grid(row=8, column=0, sticky="w", **pad)
        llm_btns = ttk.Frame(frm)
        llm_btns.grid(row=8, column=1, sticky="w", **pad)
        ttk.Button(llm_btns, text="拉取模型", command=self._fetch_models).pack(side="left")
        ttk.Button(llm_btns, text="测试连接", command=self._test_llm).pack(side="left", padx=6)
        self.llm_msg = ttk.Label(frm, text="", foreground="#6a737d")
        self.llm_msg.grid(row=13, column=0, columnspan=2, sticky="w", **pad)

        ttk.Label(frm, text="兜底模型（主模型失败先用它重试）").grid(row=9, column=0, sticky="w", **pad)
        self.fmodel_var = tk.StringVar()
        self.fmodel_cb = ttk.Combobox(frm, textvariable=self.fmodel_var, width=34)
        self.fmodel_cb.grid(row=10, column=0, sticky="w", **pad)
        ttk.Label(frm, text="留空 = 不兜底", foreground="#6a737d").grid(row=10, column=1, sticky="w", **pad)

        ttk.Label(frm, text="兜底服务（全部失败换该服务商）").grid(row=11, column=0, sticky="w", **pad)
        self.fprov_var = tk.StringVar()
        self.fprov_cb = ttk.Combobox(frm, textvariable=self.fprov_var,
                                     state="readonly", width=34)
        self.fprov_cb.grid(row=12, column=0, sticky="w", **pad)
        ttk.Label(frm, text="模型跟随其当前配置", foreground="#6a737d").grid(row=12, column=1, sticky="w", **pad)

        ttk.Separator(frm).grid(row=14, column=0, columnspan=2, sticky="ew", **pad)
        ttk.Label(frm, text="系统提示词（当前生效全文，可直接改）", font=("", 10, "bold")).grid(row=15, column=0, columnspan=2, sticky="w", **pad)
        ttk.Label(frm, text="研究方向").grid(row=16, column=0, sticky="w", **pad)
        self.focus_var = tk.StringVar()
        focus_entry = ttk.Entry(frm, textvariable=self.focus_var, width=40)
        focus_entry.grid(row=16, column=1, sticky="w", **pad)
        focus_entry.bind("<FocusOut>", lambda _e: self._maybe_refresh_prompt())
        ttk.Button(frm, text="按研究方向重生成默认", command=self._restore_default_prompt).grid(row=17, column=1, sticky="w", **pad)
        self.prompt_text = tk.Text(frm, width=66, height=10, wrap="word")
        self.prompt_text.grid(row=18, column=0, columnspan=2, sticky="ew", **pad)
        ttk.Button(frm, text="🔍 放大编辑", command=self._expand_prompt).grid(row=19, column=1, sticky="w", **pad)
        ttk.Label(frm, text="内容与默认完全一致时保存 = 跟随内置默认（以后升级自动生效）",
                  foreground="#6a737d").grid(row=20, column=0, columnspan=2, sticky="w", **pad)

        ttk.Separator(frm).grid(row=21, column=0, columnspan=2, sticky="ew", **pad)
        ttk.Label(frm, text="微信推送", font=("", 10, "bold")).grid(row=22, column=0, columnspan=2, sticky="w", **pad)
        self.push_var = tk.StringVar(value="serverchan")
        ttk.Combobox(frm, textvariable=self.push_var, state="readonly", width=16,
                     values=["serverchan", "pushplus", "wecom", "wxpusher", "smtp", "none"]).grid(
                         row=23, column=0, sticky="w", **pad)
        self.sendkey_var = tk.StringVar()
        ttk.Entry(frm, textvariable=self.sendkey_var, width=32, show="*").grid(row=23, column=1, sticky="w", **pad)
        self.push_hint = ttk.Label(frm, text="", foreground="#6a737d")
        self.push_hint.grid(row=24, column=0, columnspan=2, sticky="w", **pad)
        ttk.Button(frm, text="保存推送", command=self._save_push).grid(row=25, column=0, sticky="w", **pad)
        ttk.Button(frm, text="发送测试消息", command=self._test_push).grid(row=25, column=1, sticky="w", **pad)
        self.push_msg = ttk.Label(frm, text="", foreground="#6a737d")
        self.push_msg.grid(row=26, column=0, columnspan=2, sticky="w", **pad)

        ttk.Separator(frm).grid(row=27, column=0, columnspan=2, sticky="ew", **pad)
        ttk.Label(frm, text="数据源 API（留空不修改）", font=("", 10, "bold")).grid(row=28, column=0, columnspan=2, sticky="w", **pad)
        self.src_vars: dict[str, tk.StringVar] = {}
        self.src_hints: dict[str, ttk.Label] = {}
        rows = [("openalex_api_key", "OpenAlex Key", True),
                ("openalex_email", "OpenAlex 邮箱", False),
                ("elsevier_api_key", "Elsevier Key（Scopus+摘要）", True),
                ("crossref_email", "Crossref 邮箱", False)]
        for i, (field, label, secret) in enumerate(rows):
            ttk.Label(frm, text=label).grid(row=29 + i * 2, column=0, sticky="w", **pad)
            var = tk.StringVar()
            self.src_vars[field] = var
            ttk.Entry(frm, textvariable=var, width=40,
                      show="*" if secret else "").grid(row=29 + i * 2, column=1, sticky="w", **pad)
            hint = ttk.Label(frm, text="", foreground="#6a737d")
            hint.grid(row=30 + i * 2, column=0, columnspan=2, sticky="w", **pad)
            self.src_hints[field] = hint
        ttk.Button(frm, text="保存数据源", command=self._save_sources).grid(row=37, column=0, sticky="w", **pad)
        self.src_msg = ttk.Label(frm, text="", foreground="#6a737d")
        self.src_msg.grid(row=37, column=1, sticky="w", **pad)

        ttk.Separator(frm).grid(row=38, column=0, columnspan=2, sticky="ew", **pad)
        ttk.Label(frm, text="期刊分区（中科院 / JCR / 影响因子）", font=("", 10, "bold")).grid(
            row=39, column=0, columnspan=2, sticky="w", **pad)
        self._rank_ids = list(journal_rank.PROVIDERS) + ["off"]
        rank_choices = [f"{pid} — {journal_rank.PROVIDERS[pid]['name']}"
                        + ("（免费）" if pid == "easyscholar" else "")
                        for pid in journal_rank.PROVIDERS] + ["off — 关闭分区查询"]
        self.rank_prov_var = tk.StringVar()
        self.rank_prov_cb = ttk.Combobox(frm, textvariable=self.rank_prov_var,
                                         state="readonly", width=40, values=rank_choices)
        self.rank_prov_cb.grid(row=40, column=0, columnspan=2, sticky="w", **pad)
        self.rank_prov_cb.bind("<<ComboboxSelected>>", lambda _e: self._on_rank_provider_change())
        ttk.Label(frm, text="Key（留空不修改）").grid(row=41, column=0, sticky="w", **pad)
        self.rank_key_var = tk.StringVar()
        self.rank_key_entry = ttk.Entry(frm, textvariable=self.rank_key_var,
                                        width=40, show="*")
        self.rank_key_entry.grid(row=41, column=1, sticky="w", **pad)
        self.rank_hint = ttk.Label(frm, text="", foreground="#6a737d",
                                   wraplength=560, justify="left")
        self.rank_hint.grid(row=42, column=0, columnspan=2, sticky="w", **pad)
        ttk.Button(frm, text="保存分区设置", command=self._save_rank).grid(
            row=43, column=0, sticky="w", **pad)
        ttk.Button(frm, text="测试查询 Nature", command=self._test_rank).grid(
            row=43, column=1, sticky="w", **pad)
        self.rank_msg = ttk.Label(frm, text="", foreground="#6a737d",
                                  wraplength=560, justify="left")
        self.rank_msg.grid(row=44, column=0, columnspan=2, sticky="w", **pad)

        ttk.Separator(frm).grid(row=45, column=0, columnspan=2, sticky="ew", **pad)
        ttk.Label(frm, text="摘要翻译", font=("", 10, "bold")).grid(row=46, column=0, columnspan=2, sticky="w", **pad)
        self.trans_engine_var = tk.StringVar()
        self.trans_engine_cb = ttk.Combobox(frm, textvariable=self.trans_engine_var,
                                            state="readonly", width=34)
        self.trans_engine_cb.grid(row=47, column=0, sticky="w", **pad)
        self.trans_engine_cb.bind("<<ComboboxSelected>>", lambda _e: self._apply_engine_fields())
        ttk.Button(frm, text="测试翻译", command=self._test_translate).grid(row=47, column=1, sticky="w", **pad)
        self.trans_nofield_lbl = ttk.Label(frm, text="", foreground="#6a737d")
        self.trans_nofield_lbl.grid(row=48, column=0, columnspan=2, sticky="w", **pad)
        self.trans_vars: dict[str, tk.StringVar] = {}
        self.trans_hints: dict[str, ttk.Label] = {}
        self.trans_field_frames: dict[str, ttk.Frame] = {}
        self.trans_field_grid: dict[str, dict] = {}
        trans_rows = [f for rows in TRANS_FIELDS.values() for f in rows]
        trans_rows = list(dict.fromkeys(trans_rows))  # 去重保持顺序
        for i, (field, label, secret) in enumerate(trans_rows):
            rowf = ttk.Frame(frm)
            rowf.grid(row=49 + i, column=0, columnspan=2, sticky="ew", **pad)
            self.trans_field_grid[field] = dict(row=49 + i, column=0, columnspan=2,
                                                sticky="ew", **pad)
            ttk.Label(rowf, text=label, width=18, anchor="w").pack(side="left")
            var = tk.StringVar()
            self.trans_vars[field] = var
            ttk.Entry(rowf, textvariable=var, width=34,
                      show="*" if secret else "").pack(side="left")
            hint = ttk.Label(rowf, text="", foreground="#6a737d")
            hint.pack(side="left", padx=6)
            self.trans_hints[field] = hint
            self.trans_field_frames[field] = rowf
        ttk.Button(frm, text="保存翻译设置", command=self._save_translate).grid(row=56, column=0, sticky="w", **pad)
        self.trans_msg = ttk.Label(frm, text="", foreground="#6a737d")
        self.trans_msg.grid(row=56, column=1, sticky="w", **pad)

        ttk.Separator(frm).grid(row=57, column=0, columnspan=2, sticky="ew", **pad)
        ttk.Button(frm, text="保存 LLM 设置并关闭", command=self._save_llm_close).grid(row=58, column=0, sticky="w", **pad)

    # ---- 数据 ----
    def reload(self) -> None:
        cfg = load_config(self.cfg_path)
        self.providers = all_providers(cfg.llm)
        self.active = active_id(cfg.llm)
        values = [f"{pid} — {p['name']}" for pid, p in self.providers.items()]
        self.provider_cb["values"] = values
        idx = list(self.providers).index(self.active) if self.active in self.providers else 0
        self.provider_cb.current(idx)
        # 系统提示词：显示「当前生效全文」（自定义 or 按研究方向渲染的默认模板）
        focus = str(cfg.raw.get("research_focus", "") or "")
        self.focus_var.set(focus)
        self.prompt_text.delete("1.0", "end")
        self.prompt_text.insert("1.0", effective_system_prompt(cfg.llm, focus))
        self.push_var.set(cfg.push.get("provider", "serverchan"))
        self.push_hint.config(
            text="SendKey 已保存，留空不修改" if push_configured(cfg.push) else "未配置 SendKey/Token")
        self.sendkey_var.set("")
        for field, var in self.src_vars.items():
            var.set("")
            stored = str(cfg.raw.get(field, "") or "")
            if field.endswith("api_key"):
                self.src_hints[field].config(
                    text=f"已保存({mask_key(stored)})，留空不修改" if stored else "未配置")
            else:
                self.src_hints[field].config(text=f"当前: {stored}" if stored else "未配置")
        # 期刊分区
        rank_cfg = journal_rank.settings(cfg.raw)
        rank_idx = (self._rank_ids.index(rank_cfg["provider"])
                    if rank_cfg["provider"] in self._rank_ids else 0)
        self.rank_prov_cb.current(rank_idx)
        self.rank_key_var.set("")
        self.rank_msg.config(text="")
        self._apply_rank_fields()
        # 翻译区
        tcfg = cfg.raw.get("translate", {})
        from .translate import ENGINE_ORDER
        engine_ids = ENGINE_ORDER
        self.trans_engine_cb["values"] = [f"{e} — {ENGINES[e]['name']}" for e in engine_ids]
        cur = str(tcfg.get("engine", "llm"))
        self.trans_engine_cb.current(engine_ids.index(cur) if cur in engine_ids else 0)

        # 兜底服务下拉：只列已配 key 的服务商（含 custom），加"(不启用)"
        fb_pid = str((cfg.llm.get("fallback") or {}).get("provider_id", "") or "")
        options = ["(不启用)"]
        self._fprov_ids = [""]
        for pid, p in self.providers.items():
            if pid == "custom" and not p["base_url"]:
                continue
            if p["api_key"] or not p.get("key_required", True):
                options.append(f"{pid} — {p['name']} · {p['model'] or '?'}")
                self._fprov_ids.append(pid)
        self.fprov_cb["values"] = options
        idx = self._fprov_ids.index(fb_pid) if fb_pid in self._fprov_ids else 0
        self.fprov_cb.current(idx)
        for field, var in self.trans_vars.items():
            var.set("")
            stored = str(tcfg.get(field, "") or "")
            secret = field.endswith(("key", "secret"))
            self.trans_hints[field].config(
                text=f"已保存({mask_key(stored)})" if stored and secret
                else (f"当前: {stored}" if stored else "未配置"))
        self._apply_engine_fields()
        self._fill_provider()

    def _current_pid(self) -> str:
        return self.provider_var.get().split(" — ")[0]

    def _fill_provider(self) -> None:
        p = self.providers.get(self._current_pid())
        if not p:
            return
        self.baseurl_var.set(p["base_url"])
        self.model_cb["values"] = p["models"]
        self.model_var.set(p["model"])
        self.fmodel_cb["values"] = p["models"]
        self.fmodel_var.set(str(p.get("fallback_model", "") or ""))
        self.key_var.set("")
        self.clear_var.set(False)
        self.key_hint.config(
            text=f"已保存({mask_key(p['api_key'])})，留空不修改" if p["api_key"] else "未配置 API Key")
        self.active_var.set(self._current_pid() == self.active)
        self.llm_msg.config(text="")

    # ---- 动作 ----
    def _restore_default_prompt(self) -> None:
        """按当前「研究方向」重新生成默认提示词填回编辑框（点保存才生效）。"""
        self.prompt_text.delete("1.0", "end")
        self.prompt_text.insert("1.0", default_system_prompt(self.focus_var.get()))

    def _expand_prompt(self) -> None:
        """提示词太小看不清时：弹出独立大窗口编辑，完成写回设置框。"""
        win = tk.Toplevel(self)
        win.title("系统提示词 · 放大编辑")
        win.geometry("860x600")
        win.transient(self)
        txt = tk.Text(win, wrap="word", font=("", 10))
        scroll = ttk.Scrollbar(win, command=txt.yview)
        txt.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        txt.pack(fill="both", expand=True, padx=8, pady=8)
        txt.insert("1.0", self.prompt_text.get("1.0", "end-1c"))
        btns = ttk.Frame(win)
        btns.pack(fill="x", padx=8, pady=(0, 8))

        def done() -> None:
            self.prompt_text.delete("1.0", "end")
            self.prompt_text.insert("1.0", txt.get("1.0", "end-1c"))
            win.destroy()
        ttk.Button(btns, text="完成", command=done).pack(side="right")
        ttk.Button(btns, text="取消", command=win.destroy).pack(side="right", padx=6)
        txt.focus_set()

    def _maybe_refresh_prompt(self) -> None:
        """改了研究方向后：若编辑框当前还是默认模板，跟随新方向刷新；用户手写内容不动。"""
        current = self.prompt_text.get("1.0", "end-1c").strip()
        cfg = load_config(self.cfg_path)
        old_default = default_system_prompt(str(cfg.raw.get("research_focus", ""))).strip()
        if not current or current == old_default:
            self.prompt_text.delete("1.0", "end")
            self.prompt_text.insert("1.0", default_system_prompt(self.focus_var.get()))

    def _save_llm_close(self) -> None:
        pid = self._current_pid()
        focus = self.focus_var.get().strip()
        prompt = normalize_custom_prompt(self.prompt_text.get("1.0", "end-1c"), focus)

        def mutate(c: dict) -> None:
            apply_llm_save(c, pid, set_active=self.active_var.get(),
                           base_url=self.baseurl_var.get(), model=self.model_var.get(),
                           api_key=self.key_var.get(), clear_key=self.clear_var.get(),
                           system_prompt=prompt,
                           models=list(self.model_cb["values"]) or None,
                           fallback_model=self.fmodel_var.get(),
                           fallback_provider=self._fprov_ids[self.fprov_cb.current()]
                           if hasattr(self, "_fprov_ids") and self._fprov_ids else "")
            if focus:
                c["research_focus"] = focus
        update_config_file(self.cfg_path, mutate)
        self.app.refresh_status()
        self.destroy()

    def _test_llm(self) -> None:
        p = self.providers.get(self._current_pid(), {})
        draft = {"base_url": self.baseurl_var.get().strip() or p.get("base_url", ""),
                 "model": self.model_var.get().strip() or p.get("model", ""),
                 "api_key": self.key_var.get().strip() or p.get("api_key", "")}
        self.llm_msg.config(text="测试中…")

        def work():
            ok, msg = test_connection(draft)
            self.app.call_in_ui( lambda: self.llm_msg.config(
                text=("✔ " if ok else "✘ ") + msg,
                foreground="#116329" if ok else "#b31d28"))
        threading.Thread(target=work, daemon=True).start()

    def _fetch_models(self) -> None:
        """拉取该服务商真实模型清单（OpenAI 兼容 /models），填入下拉并随保存持久化。"""
        from .llm_providers import fetch_models
        p = self.providers.get(self._current_pid(), {})
        base_url = self.baseurl_var.get().strip() or p.get("base_url", "")
        api_key = self.key_var.get().strip() or p.get("api_key", "")
        if not base_url:
            self.llm_msg.config(text="✘ 先填 Base URL", foreground="#b31d28")
            return
        self.llm_msg.config(text="拉取模型中…")

        def work():
            try:
                models = fetch_models(base_url, api_key)
            except Exception as e:  # noqa: BLE001
                msg = str(e)[:120]
                self.app.call_in_ui( lambda: self.llm_msg.config(
                    text="✘ " + msg, foreground="#b31d28"))
                return

            def apply():
                self.model_cb["values"] = models
                self.fmodel_cb["values"] = models
                if models and self.model_var.get() not in models:
                    self.model_var.set(models[0])
                if self.fmodel_var.get() and self.fmodel_var.get() not in models:
                    self.fmodel_var.set("")
                self.llm_msg.config(text=f"✔ 拉到 {len(models)} 个模型（保存后写入配置）",
                                    foreground="#116329")
            self.app.call_in_ui( apply)
        threading.Thread(target=work, daemon=True).start()

    def _save_push(self) -> None:
        update_config_file(self.cfg_path, lambda c: apply_push_save(
            c, self.push_var.get(), self.sendkey_var.get()))
        self.push_msg.config(text="已保存 ✔", foreground="#116329")
        self.app.refresh_status()

    def _test_push(self) -> None:
        self.push_msg.config(text="发送中…")

        def work():
            ok, msg = push_wechat(load_config(self.cfg_path).push,
                                  "✅ lit-zaobao 测试消息", "GUI 设置框发出的测试消息。")
            self.app.call_in_ui( lambda: self.push_msg.config(
                text=("✔ " if ok else "✘ ") + msg,
                foreground="#116329" if ok else "#b31d28"))
        threading.Thread(target=work, daemon=True).start()

    def _save_sources(self) -> None:
        updates = {f: v.get().strip() for f, v in self.src_vars.items() if v.get().strip()}
        if updates:
            update_config_file(self.cfg_path, lambda c: c.update(updates))
        self.src_msg.config(text=f"已保存 {len(updates)} 项 ✔", foreground="#116329")
        self.reload()

    # ---- 期刊分区（仿 LitBoard：EasyScholar / OneScholar 二选一） ----
    def _current_rank_provider(self) -> str:
        return self.rank_prov_var.get().split(" — ")[0] or "off"

    def _on_rank_provider_change(self) -> None:
        self.rank_msg.config(text="")
        self._apply_rank_fields()

    def _apply_rank_fields(self) -> None:
        """按服务商显示对应 Key 的保存状态；关闭时禁用输入框。"""
        s = journal_rank.settings(load_config(self.cfg_path).raw)
        provider = self._current_rank_provider()
        spec = journal_rank.PROVIDERS.get(provider)
        if not spec:
            self.rank_key_entry.config(state="disabled")
            self.rank_hint.config(text="已关闭：日报不再标注期刊分区")
            return
        self.rank_key_entry.config(state="normal")
        saved = str(s.get(spec["key_field"], "") or "")
        if saved:
            hint = f"{spec['name']} {spec['key_label']} 已保存({mask_key(saved)})，留空不修改"
        else:
            hint = (f"未配置 {spec['name']} {spec['key_label']} · 获取: {spec['signup']}")
        self.rank_hint.config(text=hint)

    def _save_rank(self) -> None:
        update_config_file(self.cfg_path, lambda c: journal_rank.apply_rank_save(
            c, self._current_rank_provider(), self.rank_key_var.get()))
        self.rank_key_var.set("")
        self._apply_rank_fields()
        self.rank_msg.config(text="已保存 ✔", foreground="#116329")
        self.app.refresh_status()

    def _test_rank(self) -> None:
        provider = self._current_rank_provider()
        key = self.rank_key_var.get().strip()
        cfg = load_config(self.cfg_path)
        self.rank_msg.config(text="查询中…", foreground="#6a737d")

        def work():
            ok, msg = journal_rank.test_connection(cfg.raw, provider=provider, key=key or None)
            self.app.call_in_ui(lambda: self.rank_msg.config(
                text=("✔ " if ok else "✘ ") + msg,
                foreground="#116329" if ok else "#b31d28"))
        threading.Thread(target=work, daemon=True).start()

    def _current_engine(self) -> str:
        return self.trans_engine_var.get().split(" — ")[0]

    def _apply_engine_fields(self) -> None:
        """只显示当前引擎需要的凭据字段（其余隐藏），框不再一屏。"""
        spec_fields = {f for f, _l, _s in TRANS_FIELDS.get(self._current_engine(), [])}
        for field, rowf in self.trans_field_frames.items():
            if field in spec_fields:
                rowf.grid(**self.trans_field_grid[field])
            else:
                rowf.grid_remove()
        self.trans_nofield_lbl.config(
            text="" if spec_fields else "（该引擎无需额外配置）")

    def _save_translate(self) -> None:
        updates = {f: v.get().strip() for f, v in self.trans_vars.items() if v.get().strip()}

        def mutate(c: dict) -> None:
            t = c.setdefault("translate", {})
            t["engine"] = self._current_engine()
            t.update(updates)
        update_config_file(self.cfg_path, mutate)
        self.trans_msg.config(text="已保存 ✔", foreground="#116329")
        self.reload()

    def _test_translate(self) -> None:
        sample = ("Tool use and reasoning help large language model agents "
                  "plan multi-step tasks and evaluate the results.")
        cfg = load_config(self.cfg_path)
        tcfg = dict(cfg.raw.get("translate", {}))
        tcfg["engine"] = self._current_engine()
        for f, v in self.trans_vars.items():   # 表单里刚填的优先参与测试
            if v.get().strip():
                tcfg[f] = v.get().strip()
        self.trans_msg.config(text="翻译中…")

        def work():
            try:
                zh, engine = translate_text(sample, tcfg, cfg.llm)
                self.app.call_in_ui( lambda: self.trans_msg.config(
                    text=f"✔ [{ENGINES[engine]['name']}] {zh[:60]}…", foreground="#116329"))
            except Exception as e:  # noqa: BLE001
                msg = str(e)[:120]
                self.app.call_in_ui( lambda: self.trans_msg.config(
                    text="✘ " + msg, foreground="#b31d28"))
        threading.Thread(target=work, daemon=True).start()


# ------------------------------------------------------------------ 主窗
class App(tk.Tk):
    def __init__(self, config_path: Path | None = None):
        super().__init__()
        self.config_path = config_path or CONFIG_PATH
        self.cfg: Config = load_config(self.config_path)
        self.title("lit-zaobao 文献早报")
        self.geometry("1040x660")
        self.minsize(900, 560)
        self.log_q: "queue.Queue" = queue.Queue()
        self.ui_q = UiQueue()
        self.running = False
        self._current_rec: dict | None = None
        self._build()
        self.refresh_all()
        self.after(150, self._drain_log)

    def call_in_ui(self, fn) -> None:
        """工作线程安全地调度 UI 更新（经队列由主循环执行）。"""
        self.ui_q.put(fn)

    # ---- 构造 ----
    def _build(self) -> None:
        top = ttk.Frame(self)
        top.pack(fill="x", padx=10, pady=8)
        self.status_lbl = ttk.Label(top, text="…")
        self.status_lbl.pack(side="left")
        ttk.Button(top, text="退出", command=self.destroy).pack(side="right")
        self.auto_btn = ttk.Button(top, text="⏰ 每日推送", command=self._auto_dialog)
        self.auto_btn.pack(side="right", padx=6)
        ttk.Button(top, text="⚙ 设置", command=self._open_settings).pack(side="right")
        self.run_btn = ttk.Button(top, text="立即生成早报", command=self.run_now)
        self.run_btn.pack(side="right")

        paned = ttk.Panedwindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        # 左区：论文阅读 / 运行日志 双页签
        self.nb = ttk.Notebook(paned)
        paned.add(self.nb, weight=3)

        read = ttk.Frame(self.nb)
        self.nb.add(read, text=" 论文阅读 ")
        self.read_text = tk.Text(read, wrap="word", state="disabled",
                                 font=("", 10), spacing1=2, spacing3=2)
        self.read_text.tag_configure("title", font=("", 12, "bold"), spacing3=6)
        read_scroll = ttk.Scrollbar(read, command=self.read_text.yview)
        self.read_text.configure(yscrollcommand=read_scroll.set)
        self.read_text.pack(side="left", fill="both", expand=True)
        read_scroll.pack(side="right", fill="y")
        read_btns = ttk.Frame(read)
        read_btns.pack(side="bottom", fill="x", pady=4)
        self.open_url_btn = ttk.Button(read_btns, text="打开原文", command=self._open_current_url)
        self.open_url_btn.pack(side="left", padx=4)
        self.like_btn = ttk.Button(read_btns, text="❤ 点赞", command=self._toggle_like_current)
        self.like_btn.pack(side="left", padx=4)
        self.trans_btn = ttk.Button(read_btns, text="译 翻译摘要", command=self._translate_current)
        self.trans_btn.pack(side="left", padx=4)

        logf = ttk.Frame(self.nb)
        self.nb.add(logf, text=" 运行日志 ")
        self.log_text = tk.Text(logf, wrap="word", state="disabled", font=("Consolas", 9))
        log_scroll = ttk.Scrollbar(logf, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")

        # 右区：历史日报 / 论文列表 / 我的点赞
        right = ttk.Frame(paned)
        paned.add(right, weight=2)

        dg = ttk.LabelFrame(right, text="历史日报（选中加载当批论文）")
        dg.pack(fill="both", expand=True, pady=(0, 6))
        self.digest_lb = tk.Listbox(dg, height=5, exportselection=False)
        self.digest_lb.pack(fill="both", expand=True, padx=4, pady=4)
        self.digest_lb.bind("<<ListboxSelect>>", self._on_digest_select)
        self.digest_lb.bind("<Double-Button-1>", lambda _e: self._open_digest())
        ttk.Button(dg, text="打开 HTML 原版", command=self._open_digest).pack(anchor="w", padx=4, pady=(0, 4))

        cand = ttk.LabelFrame(right, text="论文列表（点选阅读，双击开原文）")
        cand.pack(fill="both", expand=True, pady=(0, 6))
        self.paper_src_lbl = ttk.Label(cand, text="", foreground="#6a737d")
        self.paper_src_lbl.pack(anchor="w", padx=4)
        self.cand_lb = tk.Listbox(cand, height=9, exportselection=False)
        self.cand_lb.pack(fill="both", expand=True, padx=4, pady=4)
        self.cand_lb.bind("<<ListboxSelect>>", self._on_paper_select)
        self.cand_lb.bind("<Double-Button-1>", lambda _e: self._open_current_url())
        ttk.Button(cand, text="❤ 点赞所选", command=self._like_selected).pack(anchor="w", padx=4, pady=(0, 4))

        lk = ttk.LabelFrame(right, text="我的点赞")
        lk.pack(fill="both", expand=True)
        self.like_lb = tk.Listbox(lk, height=4, exportselection=False)
        self.like_lb.pack(fill="both", expand=True, padx=4, pady=4)
        self.like_lb.bind("<<ListboxSelect>>", self._on_like_select)
        ttk.Button(lk, text="取消所选点赞", command=self._unlike_selected).pack(anchor="w", padx=4, pady=(0, 4))

    # ---- 数据刷新 ----
    def _out(self) -> Path:
        return self.cfg.path("output_dir")

    def _data(self) -> Path:
        return self.cfg.path("data_dir")

    def refresh_status(self) -> None:
        fresh = load_config(self.config_path)
        llm = resolve(fresh.llm)
        llm_mark = "●" if llm.get("api_key") else "◐"
        push_mark = "●" if push_configured(fresh.push) else "◐"
        likes = load_likes(self._data())
        rank_cfg = journal_rank.settings(fresh.raw)
        if journal_rank.is_configured(fresh.raw):
            rank_txt = "●" + journal_rank.provider_name(rank_cfg["provider"])
        else:
            rank_txt = "◐关闭" if rank_cfg["provider"] == "off" else "◐未配置"
        self.status_lbl.config(
            text=f"LLM: {llm_mark}{llm['name']} · {llm['model'] or '未配置'}    "
                 f"推送: {push_mark}{'已配置' if push_configured(fresh.push) else '未配置'}    "
                 f"分区: {rank_txt}    "
                 f"点赞: {len(likes)} 篇")
        # schtasks 查询是子进程调用（数百毫秒），放后台线程，避免保存设置后 UI 卡顿
        self.auto_btn.config(text="⏰ …")
        push_time = str(fresh.raw.get("push_time", "") or "08:30")

        def work() -> None:
            exists = autorun.task_exists()
            text = f"⏰ 每日 {push_time} ✔" if exists else "⏰ 每日推送: 关"
            self.call_in_ui(lambda: self.auto_btn.config(text=text))
        threading.Thread(target=work, daemon=True).start()

    def _auto_dialog(self) -> None:
        """每日自动推送：时间点可调的开关（写 Windows 计划任务，指向本 exe）。"""
        win = tk.Toplevel(self)
        win.title("每日自动推送")
        win.transient(self)
        win.grab_set()
        frm = ttk.Frame(win)
        frm.pack(padx=14, pady=12)
        enabled = autorun.task_exists()
        cfg = load_config(self.config_path)
        at_now = str(cfg.raw.get("push_time", "") or "08:30")
        ttk.Label(frm, text=f"计划任务状态: {'已开启' if enabled else '未开启'}",
                  font=("", 10, "bold")).pack(anchor="w", pady=(0, 6))
        row = ttk.Frame(frm)
        row.pack(anchor="w", pady=4)
        ttk.Label(row, text="每天").pack(side="left")
        time_var = tk.StringVar(value=at_now)
        ttk.Entry(row, textvariable=time_var, width=7).pack(side="left", padx=4)
        ttk.Label(row, text="自动抓取 → 摘要 → 推送到微信（无窗口，无需常驻任何程序）").pack(side="left")
        msg = ttk.Label(frm, text="", foreground="#6a737d", wraplength=420)
        msg.pack(anchor="w", pady=4)
        btns = ttk.Frame(frm)
        btns.pack(anchor="w", pady=(6, 0))

        def on() -> None:
            msg.config(text="处理中…")
            at = time_var.get().strip()

            def work() -> None:
                ok, m = autorun.register_task(at)
                if ok:
                    update_config_file(self.config_path,
                                       lambda c: c.update(push_time=at))
                self.call_in_ui(lambda: (
                    msg.config(text=("✔ " if ok else "✘ ") + m,
                               foreground="#116329" if ok else "#b31d28"),
                    self.refresh_status()))
            threading.Thread(target=work, daemon=True).start()

        def off() -> None:
            msg.config(text="处理中…")

            def work() -> None:
                ok, m = autorun.unregister_task()
                self.call_in_ui(lambda: (
                    msg.config(text=("✔ " if ok else "✘ ") + m,
                               foreground="#116329" if ok else "#b31d28"),
                    self.refresh_status()))
            threading.Thread(target=work, daemon=True).start()

        ttk.Button(btns, text="开启 / 更新时间", command=on).pack(side="left")
        ttk.Button(btns, text="关闭", command=off).pack(side="left", padx=6)
        ttk.Button(btns, text="完成", command=win.destroy).pack(side="left")

    def refresh_all(self) -> None:
        self.refresh_status()
        self.digest_lb.delete(0, "end")
        self._digest_files = sorted(self._out().glob("digest-*.html"), reverse=True)[:30]
        for f in self._digest_files:
            self.digest_lb.insert("end", f.stem)
        jsons = sorted(self._out().glob("digest-*.json"), reverse=True)
        self._load_papers(jsons[0] if jsons else None)
        self._refresh_likes()

    def _refresh_likes(self) -> None:
        self.like_lb.delete(0, "end")
        self._likes = load_likes(self._data())
        for x in self._likes:
            self.like_lb.insert("end", f"❤ {x.get('title', '')[:46]}")

    def _load_papers(self, json_path: Path | None) -> None:
        """把某个日报 JSON 加载进论文列表。"""
        self._papers: list[dict] = []
        if json_path and json_path.exists():
            try:
                self._papers = json.loads(json_path.read_text(encoding="utf-8"))[:20]
            except (json.JSONDecodeError, OSError):
                pass
        self.paper_src_lbl.config(
            text=f"来自 {json_path.stem}，共 {len(self._papers)} 篇" if json_path else "暂无数据")
        liked_ids = {x.get("id") for x in load_likes(self._data())}
        self.cand_lb.delete(0, "end")
        for p in self._papers:
            mark = "❤" if p.get("id") in liked_ids else " "
            self.cand_lb.insert("end", f"{mark} [{p.get('score', 0)}] {p.get('title', '')[:44]}")

    # ---- 阅读页 ----
    def _show_paper(self, rec: dict) -> None:
        self._current_rec = rec
        liked = any(x.get("id") == rec.get("id") for x in load_likes(self._data()))
        translation = cache_lookup(str(rec.get("abstract", "")), self._data())
        text = format_paper_detail(rec, liked, translation_zh=translation)
        first_nl = text.find("\n")
        self.read_text.configure(state="normal")
        self.read_text.delete("1.0", "end")
        self.read_text.insert("1.0", text)
        if first_nl > 0:
            self.read_text.tag_add("title", "1.0", f"1.{first_nl}")
        self.read_text.configure(state="disabled")
        self.like_btn.config(text="💔 取消点赞" if liked else "❤ 点赞")
        has_abs = bool(str(rec.get("abstract", "")).strip())
        self.trans_btn.config(state="disabled" if (not has_abs or translation) else "normal",
                              text="✔ 已翻译" if translation else "译 翻译摘要")
        self.nb.select(0)

    def _translate_current(self) -> None:
        rec = self._current_rec
        abstract = str((rec or {}).get("abstract", "")).strip()
        if not rec or not abstract:
            return
        self.trans_btn.config(state="disabled", text="翻译中…")
        cfg_path = self.config_path

        def work():
            cfg = load_config(cfg_path)
            try:
                zh, engine = cached_translate(abstract, cfg.raw.get("translate", {}),
                                              cfg.llm, self._data())
                self.call_in_ui( lambda: self._show_paper(rec))
            except Exception as e:  # noqa: BLE001
                msg = str(e)[:150]
                self.call_in_ui( lambda: (
                    self.trans_btn.config(state="normal", text="译 翻译摘要"),
                    messagebox.showerror("翻译失败", msg)))
        threading.Thread(target=work, daemon=True).start()

    # ---- 事件 ----
    def _on_digest_select(self, _e=None) -> None:
        sel = self.digest_lb.curselection()
        if not sel:
            return
        stem = self._digest_files[sel[0]].stem
        self._load_papers(self._out() / f"{stem}.json")

    def _on_paper_select(self, _e=None) -> None:
        sel = self.cand_lb.curselection()
        if sel:
            self._show_paper(self._papers[sel[0]])

    def _on_like_select(self, _e=None) -> None:
        sel = self.like_lb.curselection()
        if sel:
            self._show_paper(self._likes[sel[0]])

    def _open_current_url(self) -> None:
        rec = self._current_rec
        if rec is None and self.cand_lb.curselection():
            rec = self._papers[self.cand_lb.curselection()[0]]
        if rec and rec.get("url"):
            webbrowser.open(str(rec["url"]))

    def _toggle_like_current(self) -> None:
        rec = self._current_rec
        if not rec:
            return
        if any(x.get("id") == rec.get("id") for x in load_likes(self._data())):
            remove_like(self._data(), str(rec.get("id", "")))
        else:
            fields = {k: rec.get(k, "") for k in
                      ("id", "source", "title", "authors", "abstract", "url",
                       "published", "doi", "journal")}
            save_like(self._data(), Paper(**fields))
        self.refresh_all()
        self._show_paper(rec)

    # ---- 动作 ----
    def run_now(self) -> None:
        if self.running:
            return
        self.running = True
        self.run_btn.config(state="disabled", text="运行中…")
        self.nb.select(1)  # 切到运行日志页签

        def worker() -> None:
            from contextlib import redirect_stdout
            try:
                with redirect_stdout(LogWriter(self.log_q)):
                    run_digest(load_config(self.config_path))
            except Exception as e:  # noqa: BLE001
                self.log_q.put(f"\n[错误] {e}\n")
            self.log_q.put(None)

        threading.Thread(target=worker, daemon=True).start()

    def _drain_log(self) -> None:
        if not self.winfo_exists():   # 窗口销毁后不再续期，防 Tcl 报错/闪退
            return
        # 先执行工作线程排队的 UI 更新，再追加日志
        for fn in self.ui_q.drain():
            try:
                fn()
            except Exception:  # noqa: BLE001 - 单个回调失败不拖垮主循环
                pass
        try:
            while True:
                item = self.log_q.get_nowait()
                if item is None:
                    self.running = False
                    self.run_btn.config(state="normal", text="立即生成早报")
                    self.refresh_all()
                    continue
                self.log_text.configure(state="normal")
                self.log_text.insert("end", item)
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(150, self._drain_log)

    def _open_digest(self) -> None:
        sel = self.digest_lb.curselection()
        if sel:
            f = self._digest_files[sel[0]]
            webbrowser.open(f.resolve().as_uri())

    def _like_selected(self) -> None:
        sel = self.cand_lb.curselection()
        if not sel:
            return
        rec = self._papers[sel[0]]
        fields = {k: rec.get(k, "") for k in
                  ("id", "source", "title", "authors", "abstract", "url",
                   "published", "doi", "journal")}
        save_like(self._data(), Paper(**fields))
        self.refresh_all()
        self._show_paper(rec)

    def _unlike_selected(self) -> None:
        sel = self.like_lb.curselection()
        if not sel:
            return
        remove_like(self._data(), str(self._likes[sel[0]].get("id", "")))
        self.refresh_all()

    def _open_settings(self) -> None:
        SettingsDialog(self)


# ------------------------------------------------------------------ 入口
def _acquire_lock(port: int = _LOCK_PORT) -> socket.socket | None:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        s.listen(1)
        return s
    except OSError:
        s.close()   # 绑定失败必须显式关闭，否则泄漏 fd
        return None


def main() -> None:
    lock = _acquire_lock()
    if lock is None:
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo("lit-zaobao", "lit-zaobao 已在运行中")
        root.destroy()
        return
    # config.json 损坏时：备份后用默认配置继续，而不是静默退出（闪退观感）
    try:
        load_config(CONFIG_PATH)
    except SystemExit as e:
        from datetime import datetime
        backup = CONFIG_PATH.with_suffix(f".bad-{datetime.now():%Y%m%d%H%M%S}.json")
        try:
            CONFIG_PATH.replace(backup)
        except OSError:
            pass
        root = tk.Tk()
        root.withdraw()
        messagebox.showwarning(
            "lit-zaobao",
            f"config.json 解析失败，已备份为 {backup.name} 并用默认配置启动。\n"
            f"请重新在设置里填写密钥。\n\n原因: {e}")
        root.destroy()
    App().mainloop()


if __name__ == "__main__":
    main()
