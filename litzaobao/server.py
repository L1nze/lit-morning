"""零依赖本地迷你控制台：`python bot.py serve [--port 8000] [--open]`。

- 只监听 127.0.0.1；文件名白名单防目录穿越；POST body 限长；密钥不出服务端（页面只见掩码）。
- 功能：浏览历史日报、点赞管理、手动触发运行（后台线程 + 日志轮询）、LitBoard 式设置框
  （服务商 / Base URL / API Key / 模型 / 测试连接 / 推送通道 / 系统提示词）。
"""
from __future__ import annotations

import io
import json
import os
import re
import threading
import webbrowser
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import __version__, journal_rank
from .config import CONFIG_PATH, Config, load_config, update_config_file
from .digest import run_digest
from .llm_providers import (active_id, all_providers, apply_llm_save, mask_key,
                            provider_status, resolve, test_connection)
from .paper import Paper
from .profile import load_likes, remove_like, save_like
from .push import apply_push_save, push_configured, push_wechat
from .summarize import (default_system_prompt, effective_system_prompt,
                        normalize_custom_prompt)
from .translate import ENGINES, translate_text

_SAFE_FILE = re.compile(r"^(?:digest|weekly)-[\w.\-]+\.(?:html|md|json)$", re.IGNORECASE)
_MAX_BODY = 32 * 1024

_STATE = {"running": False, "log": "", "started": ""}

_CSS = """
:root{--bg:#f6f7f9;--card:#fff;--text:#1f2328;--muted:#6a737d;--accent:#0969da;--border:#d8dee4;--ok:#116329;}
@media (prefers-color-scheme: dark){:root{--bg:#0d1117;--card:#161b22;--text:#e6edf3;--muted:#8b949e;--accent:#58a6ff;--border:#30363d;--ok:#7ee787;}}
*{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;line-height:1.6}
.wrap{max-width:860px;margin:0 auto;padding:24px 16px}
h1{font-size:1.3em;margin:0 0 4px} .sub{color:var(--muted);font-size:.85em;margin-bottom:16px}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px 16px;margin-bottom:12px}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:8px 0}
a{color:var(--accent);text-decoration:none} a:hover{text-decoration:underline}
button{background:var(--accent);color:#fff;border:0;border-radius:8px;padding:6px 14px;font-size:.85em;cursor:pointer}
button.ghost{background:transparent;color:var(--accent);border:1px solid var(--accent)}
button:disabled{opacity:.5;cursor:default}
input,select,textarea{background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:6px 10px;font-size:.9em}
.muted{color:var(--muted);font-size:.82em}
pre{background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:10px;font-size:.78em;white-space:pre-wrap;max-height:280px;overflow:auto}
.ok{color:var(--ok)} .dot{font-size:.8em;margin-right:4px}
ul.tight{margin:6px 0;padding-left:20px} ul.tight li{margin:4px 0}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:10;display:none}
.modal .box{max-width:580px;margin:36px auto;max-height:88vh;overflow:auto}
.hr{border:0;border-top:1px solid var(--border);margin:12px 0}
"""

_JS = r"""
async function j(url,opt){const r=await fetch(url,opt);return r.json();}
function gv(id){return document.getElementById(id).value.trim();}
function gvEl(id){return document.getElementById(id);}
async function postLike(id,unlike){
  await j('/api/'+(unlike?'unlike':'like'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
  location.reload();}
let running=false;
async function poll(){
  const s=await j('/api/status');
  gvEl('log').textContent=s.log||'(暂无日志)';
  const btn=gvEl('runbtn');
  if(s.running!==running){running=s.running;btn.disabled=running;btn.textContent=running?'运行中…':'立即生成早报';}}
async function runNow(){await j('/api/run',{method:'POST'});poll();}
setInterval(poll,2000);poll();

/* ---------- 设置框 ---------- */
let SETTINGS=null;
async function openSettings(){
  SETTINGS=await j('/api/settings');
  const sel=gvEl('sp_provider');sel.innerHTML='';
  SETTINGS.providers.forEach(p=>{const o=document.createElement('option');o.value=p.id;
    o.textContent=p.name+' ('+p.id+')';if(p.id===SETTINGS.active)o.selected=true;sel.appendChild(o);});
  gvEl('sp_prompt').value=SETTINGS.system_prompt_effective||'';
  gvEl('sp_focus').value=SETTINGS.research_focus||'';
  gvEl('sp_push').value=SETTINGS.push.provider;
  gvEl('sp_sendkey').value='';
  gvEl('sp_sendkey').placeholder=SETTINGS.push.has_key?'已保存，留空则不修改':'未配置';
  gvEl('sp_pushmsg').textContent='';
  const so=SETTINGS.sources||{};
  gvEl('sp_oa_key').value='';gvEl('sp_oa_key').placeholder=so.openalex_key_hint||'未配置';
  gvEl('sp_els_key').value='';gvEl('sp_els_key').placeholder=so.elsevier_key_hint||'未配置';
  gvEl('sp_oa_mail').value=so.openalex_email||'';
  gvEl('sp_cr_mail').value=so.crossref_email||'';
  gvEl('sp_srcmsg').textContent='';
  const rk=SETTINGS.rank||{provider:'easyscholar'};
  gvEl('sp_rank').value=rk.provider||'easyscholar';
  gvEl('sp_rankmsg').textContent='';
  applyRankFields();
  const te=SETTINGS.translate||{engine:'llm',engines:{}};
  const tsel=gvEl('sp_tengine');tsel.innerHTML='';
  Object.keys(te.engines).forEach(e=>{const o=document.createElement('option');o.value=e;
    o.textContent=te.engines[e];if(e===te.engine)o.selected=true;tsel.appendChild(o);});
  const fsel=gvEl('sp_fprov');fsel.innerHTML='';
  const savedFb=SETTINGS.fallback_provider||'';
  [''].concat(SETTINGS.providers.filter(p=>p.has_api_key).map(p=>p.id)).forEach(pid=>{
    const o=document.createElement('option');o.value=pid;
    o.textContent=pid?(''+pid+'（兜底）'):'（不启用）';if(pid===savedFb)o.selected=true;fsel.appendChild(o);});
  gvEl('sp_t_deepl').value='';gvEl('sp_t_deepl').placeholder=te.hints&&te.hints.deepl_api_key?te.hints.deepl_api_key:'未配置';
  gvEl('sp_t_bkey').value='';gvEl('sp_t_bkey').placeholder=te.hints&&te.hints.baidu_key?te.hints.baidu_key:'未配置';
  gvEl('sp_t_ysec').value='';gvEl('sp_t_ysec').placeholder=te.hints&&te.hints.youdao_secret?te.hints.youdao_secret:'未配置';
  gvEl('sp_t_baid').value=te.baidu_appid||'';gvEl('sp_t_ykey').value=te.youdao_appkey||'';
  gvEl('sp_t_mymail').value=te.mymemory_email||'';
  gvEl('sp_tmsg').textContent='';
  applyTransFields();
  fillProvider();
  gvEl('settingsModal').style.display='block';}
function closeSettings(){gvEl('settingsModal').style.display='none';}
function curProvider(){return SETTINGS.providers.find(p=>p.id===gvEl('sp_provider').value);}
function fillProvider(){const p=curProvider();if(!p)return;
  gvEl('sp_baseurl').value=p.base_url||'';
  const dl=gvEl('sp_models');dl.innerHTML='';
  (p.models||[]).forEach(m=>{const o=document.createElement('option');o.value=m;dl.appendChild(o);});
  gvEl('sp_model').value=p.model||'';
  gvEl('sp_fmodel').value=p.fallback_model||'';
  gvEl('sp_key').value='';gvEl('sp_clearkey').checked=false;
  gvEl('sp_key').placeholder=p.has_api_key?('已保存('+p.key_hint+')，留空则不修改'):'未配置';
  gvEl('sp_active').checked=(p.id===SETTINGS.active);
  gvEl('sp_llmmsg').textContent='';}
async function saveLlm(){const p=curProvider();
  const body={provider_id:p.id,base_url:gv('sp_baseurl'),model:gv('sp_model'),
    set_active:gvEl('sp_active').checked,clear_key:gvEl('sp_clearkey').checked,
    system_prompt:gvEl('sp_prompt').value,research_focus:gv('sp_focus'),
    fallback_model:gv('sp_fmodel'),fallback_provider:gvEl('sp_fprov').value};
  const k=gv('sp_key');if(k)body.api_key=k;
  body.models=[...document.querySelectorAll('#sp_models option')].map(o=>o.value);
  const r=await j('/api/settings/llm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  gvEl('sp_llmmsg').textContent=r.ok?'已保存 ✔':'保存失败';
  if(r.ok){SETTINGS=await j('/api/settings');fillProvider();setTimeout(()=>location.reload(),700);}}
async function restorePrompt(){
  const r=await j('/api/settings/prompt/default',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({focus:gv('sp_focus')})});
  if(r.ok)gvEl('sp_prompt').value=r.text;}
async function testLlm(){const p=curProvider();gvEl('sp_llmmsg').textContent='测试中…';
  const body={provider_id:p.id,base_url:gv('sp_baseurl'),model:gv('sp_model')};
  const k=gv('sp_key');if(k)body.api_key=k;
  const r=await j('/api/settings/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  gvEl('sp_llmmsg').textContent=(r.ok?'✔ ':'✘ ')+r.msg;}
async function fetchModels(){const p=curProvider();gvEl('sp_llmmsg').textContent='拉取模型中…';
  const body={provider_id:p.id,base_url:gv('sp_baseurl')};const k=gv('sp_key');if(k)body.api_key=k;
  const r=await j('/api/settings/models',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if(r.ok){const dl=gvEl('sp_models');dl.innerHTML='';r.models.forEach(m=>{const o=document.createElement('option');o.value=m;dl.appendChild(o);});
    gvEl('sp_llmmsg').textContent='✔ 拉到 '+r.models.length+' 个模型（保存后写入配置）';}
  else gvEl('sp_llmmsg').textContent='✘ '+r.msg;}
async function savePush(){
  const body={provider:gv('sp_push')};const k=gv('sp_sendkey');if(k)body.key=k;
  const r=await j('/api/settings/push',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  gvEl('sp_pushmsg').textContent=r.ok?'已保存 ✔':'保存失败';}
async function saveSources(){
  const body={openalex_api_key:gv('sp_oa_key'),elsevier_api_key:gv('sp_els_key'),
    openalex_email:gv('sp_oa_mail'),crossref_email:gv('sp_cr_mail')};
  const r=await j('/api/settings/sources',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  gvEl('sp_srcmsg').textContent=r.ok?('已保存 '+r.updated+' 项 ✔'):'保存失败';}
/* ---------- 期刊分区 ---------- */
function rankHint(p){
  const r=SETTINGS.rank||{};
  return p==='easyscholar'?(r.hint_easyscholar||''):p==='onescholar'?(r.hint_onescholar||''):'';}
function applyRankFields(){
  const p=gvEl('sp_rank').value,el=gvEl('sp_rankkey'),hint=rankHint(p);
  el.value='';el.disabled=(p==='off');
  el.placeholder=p==='off'?'（已关闭分区查询）':(hint?('已保存('+hint+')，留空不改'):'未配置，填 Key 后保存');}
async function saveRank(){
  const body={provider:gv('sp_rank'),key:gv('sp_rankkey')};
  const r=await j('/api/settings/rank',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  gvEl('sp_rankmsg').textContent=r.ok?'已保存 ✔':'保存失败';
  if(r.ok){SETTINGS=await j('/api/settings');applyRankFields();}}
async function testRank(){
  gvEl('sp_rankmsg').textContent='查询中…';
  const body={provider:gv('sp_rank'),key:gv('sp_rankkey')};
  const r=await j('/api/settings/rank/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  gvEl('sp_rankmsg').textContent=(r.ok?'✔ ':'✘ ')+r.msg;}
const TFMAP={llm:[],volc:[],tencent:[],google:[],mymemory:['mymemory'],deepl:['deepl'],baidu:['baidu'],youdao:['youdao']};
function applyTransFields(){const e=gv('sp_tengine');
  document.querySelectorAll('[data-tf]').forEach(el=>{el.style.display=(TFMAP[e]||[]).includes(el.dataset.tf)?'':'none';});
  gvEl('sp_t_nofield').style.display=(TFMAP[e]||[]).length?'none':'';}
async function saveTranslate(){
  const body={engine:gv('sp_tengine'),deepl_api_key:gv('sp_t_deepl'),baidu_appid:gv('sp_t_baid'),
    baidu_key:gv('sp_t_bkey'),youdao_appkey:gv('sp_t_ykey'),youdao_secret:gv('sp_t_ysec'),
    mymemory_email:gv('sp_t_mymail')};
  const r=await j('/api/settings/translate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  gvEl('sp_tmsg').textContent=r.ok?'已保存 ✔':'保存失败';}
async function testTranslate(){gvEl('sp_tmsg').textContent='翻译中…';
  const r=await j('/api/settings/translate/test',{method:'POST'});
  gvEl('sp_tmsg').textContent=(r.ok?'✔ ':'✘ ')+r.msg;}
async function testPush(){gvEl('sp_pushmsg').textContent='发送中…';
  const r=await j('/api/settings/push/test',{method:'POST'});
  gvEl('sp_pushmsg').textContent=(r.ok?'✔ ':'✘ ')+r.msg;}
"""

_MODAL = """
<div id=settingsModal class=modal onclick="if(event.target===this)closeSettings()">
 <div class="card box">
  <div class=row style="justify-content:space-between"><b>⚙ 设置</b>
   <button class=ghost onclick="closeSettings()">✕</button></div>
  <hr class=hr>
  <b>LLM 服务商</b>
  <div class=row><select id=sp_provider onchange="fillProvider()" style="min-width:220px"></select>
   <label class=muted><input type=checkbox id=sp_active> 设为当前生效</label></div>
  <div class=row><label style="flex:1">Base URL<br>
   <input id=sp_baseurl style="width:100%" placeholder="https://.../v1"></label></div>
  <div class=row><label style="flex:1">API Key<br>
   <input id=sp_key type=password style="width:100%"></label>
   <label class=muted style="margin-top:18px"><input type=checkbox id=sp_clearkey> 清除</label></div>
  <div class=row><label style="flex:1">模型<br>
   <input id=sp_model list=sp_models style="width:100%"><datalist id=sp_models></datalist></label>
   <button class=ghost onclick="fetchModels()">拉取模型</button></div>
  <div class=row><label style="flex:1">兜底模型（主模型失败先用它重试）<br>
   <input id=sp_fmodel list=sp_models style="width:100%"></label>
   <label style="flex:1">兜底服务（全部失败换该服务商）<br>
   <select id=sp_fprov style="width:100%"></select></label></div>
  <div class=row><button onclick="testLlm()">测试连接</button>
   <button onclick="saveLlm()">保存</button><span id=sp_llmmsg class=muted></span></div>
  <hr class=hr>
  <b>系统提示词</b> <span class=muted>（当前生效全文，可直接改）</span>
  <div class=row><label style="flex:1">研究方向<br>
   <input id=sp_focus style="width:100%" placeholder="如：大语言模型与智能体（LLM Agents）"></label>
   <button class=ghost onclick="restorePrompt()">按方向重生成默认</button></div>
  <div class=row><textarea id=sp_prompt style="width:100%;min-height:140px"></textarea></div>
  <div class=row><span class=muted>与默认完全一致时保存 = 跟随内置默认（升级自动生效）</span></div>
  <hr class=hr>
  <b>数据源 API</b> <span class=muted>（留空不修改）</span>
  <div class=row><label style="flex:1">OpenAlex Key<br>
   <input id=sp_oa_key type=password style="width:100%"></label>
   <label style="flex:1">OpenAlex 邮箱<br>
   <input id=sp_oa_mail style="width:100%"></label></div>
  <div class=row><label style="flex:1">Elsevier Key（Scopus 检索 + 摘要回填）<br>
   <input id=sp_els_key type=password style="width:100%"></label>
   <label style="flex:1">Crossref 邮箱<br>
   <input id=sp_cr_mail style="width:100%"></label></div>
  <div class=row><button onclick="saveSources()">保存数据源</button><span id=sp_srcmsg class=muted></span></div>
  <hr class=hr>
  <b>期刊分区</b> <span class=muted>（中科院分区 / JCR / 影响因子 / Top；推送时自动标注）</span>
  <div class=row><label style="flex:1">服务商<br>
   <select id=sp_rank style="width:100%" onchange="applyRankFields()">
    <option value=easyscholar>EasyScholar（免费，≤2 次/秒）</option>
    <option value=onescholar>OneScholar（SciGreat 会员）</option>
    <option value=off>关闭</option></select></label>
   <label style="flex:1">Key（留空不修改）<br>
   <input id=sp_rankkey type=password style="width:100%"></label></div>
  <div class=row><button onclick="saveRank()">保存</button>
   <button class=ghost onclick="testRank()">测试查询 Nature</button><span id=sp_rankmsg class=muted></span></div>
  <hr class=hr>
  <b>摘要翻译</b>
  <div class=row><select id=sp_tengine style="min-width:220px" onchange="applyTransFields()"></select>
   <button class=ghost onclick="testTranslate()">测试翻译</button><span id=sp_tmsg class=muted></span></div>
  <div class=row id=sp_t_nofield style="display:none"><span class=muted>（该引擎无需额外配置）</span></div>
  <div class=row><label style="flex:1" data-tf="deepl">DeepL Key<br><input id=sp_t_deepl type=password style="width:100%"></label>
   <label style="flex:1" data-tf="mymemory">MyMemory 邮箱(提额)<br><input id=sp_t_mymail style="width:100%"></label></div>
  <div class=row><label style="flex:1" data-tf="baidu">百度 AppID<br><input id=sp_t_baid style="width:100%"></label>
   <label style="flex:1" data-tf="baidu">百度 Key<br><input id=sp_t_bkey type=password style="width:100%"></label></div>
  <div class=row><label style="flex:1" data-tf="youdao">有道 AppKey<br><input id=sp_t_ykey style="width:100%"></label>
   <label style="flex:1" data-tf="youdao">有道 Secret<br><input id=sp_t_ysec type=password style="width:100%"></label></div>
  <div class=row><button onclick="saveTranslate()">保存翻译设置</button></div>
  <hr class=hr>
  <b>微信推送</b>
  <div class=row><select id=sp_push>
    <option value=serverchan>Server酱</option><option value=pushplus>PushPlus</option>
    <option value=wecom>企业微信</option><option value=wxpusher>WxPusher</option>
    <option value=smtp>邮件</option>
    <option value=none>关闭</option></select>
   <input id=sp_sendkey type=password placeholder="SendKey / Token" style="flex:1"></div>
  <div class=row><button onclick="savePush()">保存</button>
   <button class=ghost onclick="testPush()">发送测试消息</button><span id=sp_pushmsg class=muted></span></div>
 </div>
</div>
"""


def _page(title: str, body: str) -> bytes:
    return (f"<!doctype html><html lang=zh-CN><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{title}</title><style>{_CSS}</style></head><body><div class=wrap>"
            f"{body}</div>{_MODAL}<script>{_JS}</script></body></html>").encode("utf-8")


def _esc(s: str) -> str:
    import html as h
    return h.escape(str(s), quote=True)


class _Handler(BaseHTTPRequestHandler):
    cfg: Config = None          # 由 make_server 注入
    config_path: Path = CONFIG_PATH

    # ---- 工具 ----
    def _send(self, code: int, data: bytes, ctype: str = "text/html; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > _MAX_BODY:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, ValueError):
            return {}

    def log_message(self, fmt, *args):  # 静音默认访问日志
        pass

    # ---- 数据 ----
    def _out(self) -> Path:
        return self.cfg.path("output_dir")

    def _data(self) -> Path:
        return self.cfg.path("data_dir")

    def _digest_files(self) -> list[Path]:
        return sorted(self._out().glob("digest-*.html"), reverse=True)[:30]

    def _latest_papers(self) -> list[dict]:
        files = sorted(self._out().glob("digest-*.json"), reverse=True)
        if not files:
            return []
        try:
            return json.loads(files[0].read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []

    # ---- 路由 ----
    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == "/":
            return self._home()
        if path.startswith("/digest/"):
            return self._serve_file(path.rsplit("/", 1)[-1])
        if path == "/api/likes":
            return self._json(load_likes(self._data()))
        if path == "/api/status":
            return self._json({**_STATE, "app": "lit-zaobao", "version": __version__})
        if path == "/api/settings":
            return self._get_settings()
        return self._send(404, _page("404", "<h1>404</h1>"))

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        body = self._body()
        routes = {
            "/api/run": lambda: self._start_run(),
            "/api/like": lambda: self._like(body.get("id", "")),
            "/api/unlike": lambda: self._unlike(body.get("id", "")),
            "/api/settings/llm": lambda: self._save_llm(body),
            "/api/settings/test": lambda: self._test_llm(body),
            "/api/settings/push": lambda: self._save_push(body),
            "/api/settings/push/test": lambda: self._test_push(),
            "/api/settings/sources": lambda: self._save_sources(body),
            "/api/settings/rank": lambda: self._save_rank(body),
            "/api/settings/rank/test": lambda: self._test_rank(body),
            "/api/settings/translate": lambda: self._save_translate(body),
            "/api/settings/translate/test": lambda: self._test_translate(),
            "/api/settings/prompt/default": lambda: self._prompt_default(body),
            "/api/settings/models": lambda: self._fetch_models(body),
            "/api/shutdown": lambda: self._shutdown(),
        }
        handler = routes.get(path)
        if handler:
            return handler()
        return self._json({"ok": False, "error": "unknown route"}, 404)

    # ---- 页面 ----
    def _home(self) -> None:
        # 状态行每次重读配置（设置框保存后立刻生效）；路径仍用启动时的 cfg
        fresh = load_config(self.config_path)
        llm = resolve(fresh.llm)
        llm_ok = "●" if llm.get("api_key") else "◐"
        push_ok = "●" if push_configured(fresh.push) else "◐"
        rank_ok = journal_rank.is_configured(fresh.raw)
        rank_cfg = journal_rank.settings(fresh.raw)
        rank_display = (journal_rank.provider_name(rank_cfg["provider"]) if rank_ok
                        else "关闭" if rank_cfg["provider"] == "off" else "未配置")
        likes = load_likes(self._data())
        digests = self._digest_files()
        papers = self._latest_papers()
        liked_ids = {x.get("id") for x in likes}

        digest_items = "".join(
            f"<li><a href='/digest/{f.name}' target='_blank'>{f.stem}</a></li>"
            for f in digests) or "<li class=muted>还没有日报，先点上方按钮生成一份</li>"
        like_items = "".join(
            f"<li>❤ {_esc(x.get('title','')[:70])} "
            f"<button class=ghost onclick=\"postLike('{_esc(x.get('id',''))}',1)\">取消</button></li>"
            for x in likes) or "<li class=muted>暂无点赞</li>"
        def _paper_item(p: dict) -> str:
            rank_text = journal_rank.summary(p.get("journal_rank"))
            rank_html = f" <span class=muted>[{_esc(rank_text)}]</span>" if rank_text else ""
            title = _esc(p.get("title", "")[:70])
            if p.get("id") in liked_ids:
                action = "<span class='ok muted'>已赞</span>"
            else:
                action = (f"<button class=ghost onclick=\"postLike('{_esc(p.get('id',''))}')\">"
                          "点赞</button>")
            return f"<li>{title}{rank_html} {action}</li>"

        paper_items = "".join(
            _paper_item(p) for p in papers[:12]) or "<li class=muted>暂无候选（先生成日报）</li>"

        body = f"""
<h1>📚 lit-zaobao 控制台</h1>
<div class=sub>{_esc(fresh.raw.get("app_tagline") or "文献早报")} · 本地控制台（仅 127.0.0.1）</div>
<div class=card><div class=row>
  <button id=runbtn onclick="runNow()">立即生成早报</button>
  <button class=ghost onclick="openSettings()">⚙ 设置</button>
  <span class=muted>LLM: <span class=dot>{llm_ok}</span>{_esc(llm['name'])} · {_esc(llm['model'] or '未配置')}
  　微信推送: <span class=dot>{push_ok}</span>{'已配置' if push_configured(fresh.push) else '未配置'}
  　期刊分区: <span class=dot>{'●' if rank_ok else '◐'}</span>{_esc(rank_display)}
  　点赞: {len(likes)} 篇</span>
  <button class=ghost style="margin-left:auto" onclick="if(confirm('退出 lit-zaobao？')){{j('/api/shutdown',{{method:'POST'}});document.body.innerHTML='<div class=wrap><h1>已退出，可以关闭本窗口了</h1></div>'}}">退出程序</button>
</div></div>
<div class=card><b>运行日志</b><pre id=log>(加载中…)</pre></div>
<div class=card><b>历史日报</b><ul class=tight>{digest_items}</ul></div>
<div class=card><b>最新一批候选（可点赞，影响后续排序）</b><ul class=tight>{paper_items}</ul></div>
<div class=card><b>我的点赞</b><ul class=tight>{like_items}</ul></div>
"""
        self._send(200, _page("lit-zaobao 控制台", body))

    def _serve_file(self, name: str) -> None:
        if not _SAFE_FILE.match(name):
            return self._send(404, b"not found", "text/plain; charset=utf-8")
        f = (self._out() / name).resolve()
        if self._out().resolve() not in f.parents or not f.exists():
            return self._send(404, b"not found", "text/plain; charset=utf-8")
        ctype = ("text/html; charset=utf-8" if f.suffix == ".html"
                 else "text/plain; charset=utf-8" if f.suffix == ".md"
                 else "application/json; charset=utf-8")
        self._send(200, f.read_bytes(), ctype)

    # ---- 动作 ----
    def _start_run(self):
        if _STATE["running"]:
            return self._json({"ok": False, "error": "already running"}, 409)
        config_path = self.config_path

        def worker():
            _STATE.update(running=True, log="", started="")
            buf = io.StringIO()
            try:
                with redirect_stdout(buf):
                    run_digest(load_config(config_path))
            except Exception as e:  # noqa: BLE001
                buf.write(f"\n[错误] {e}")
            _STATE.update(running=False, log=buf.getvalue()[-4000:])

        threading.Thread(target=worker, daemon=True).start()
        return self._json({"ok": True})

    def _like(self, pid: str):
        rec = next((p for p in self._latest_papers() if p.get("id") == pid), None)
        if not rec:
            return self._json({"ok": False, "error": "paper not found"}, 404)
        fields = {k: rec.get(k, "") for k in
                  ("id", "source", "title", "authors", "abstract", "url",
                   "published", "doi", "journal")}
        n = save_like(self._data(), Paper(**fields))
        return self._json({"ok": True, "total": n})

    def _unlike(self, pid: str):
        return self._json({"ok": remove_like(self._data(), pid)})

    # ---- 设置框 API ----
    def _get_settings(self):
        cfg = load_config(self.config_path)
        rank = journal_rank.settings(cfg.raw)
        providers = []
        for pid, p in all_providers(cfg.llm).items():
            providers.append({
                "id": pid, "name": p["name"], "base_url": p["base_url"],
                "models": p["models"], "model": p["model"],
                "fallback_model": str(p.get("fallback_model", "") or ""),
                "key_required": p["key_required"], "status": provider_status(p),
                "has_api_key": bool(p["api_key"]), "key_hint": mask_key(p["api_key"]),
            })
        tcfg = cfg.raw.get("translate", {})
        return self._json({
            "active": active_id(cfg.llm),
            "providers": providers,
            "fallback_provider": str((cfg.llm.get("fallback") or {}).get("provider_id", "") or ""),
            "push": {"provider": cfg.push.get("provider", "serverchan"),
                     "has_key": push_configured(cfg.push)},
            "sources": {
                "openalex_key_hint": mask_key(cfg.raw.get("openalex_api_key", "")) or "",
                "elsevier_key_hint": mask_key(cfg.raw.get("elsevier_api_key", "")) or "",
                "openalex_email": str(cfg.raw.get("openalex_email", "") or ""),
                "crossref_email": str(cfg.raw.get("crossref_email", "") or ""),
            },
            "rank": {
                "provider": rank["provider"],
                "cache_days": rank["cache_days"],
                "hint_easyscholar": mask_key(str(rank.get("easyscholar_key", "") or "")) or "",
                "hint_onescholar": mask_key(str(rank.get("onescholar_key", "") or "")) or "",
            },
            "translate": {
                "engine": str(tcfg.get("engine", "llm")),
                "engines": {eid: spec["name"] for eid, spec in ENGINES.items()},
                "hints": {f: mask_key(str(tcfg.get(f, "") or "")) or ""
                          for f in ("deepl_api_key", "baidu_key", "youdao_secret")},
                "baidu_appid": str(tcfg.get("baidu_appid", "") or ""),
                "youdao_appkey": str(tcfg.get("youdao_appkey", "") or ""),
                "mymemory_email": str(tcfg.get("mymemory_email", "") or ""),
            },
            "system_prompt": str(cfg.llm.get("system_prompt", "") or ""),
            "system_prompt_effective": effective_system_prompt(
                cfg.llm, cfg.raw.get("research_focus", "")),
            "research_focus": str(cfg.raw.get("research_focus", "") or ""),
            "app_tagline": str(cfg.raw.get("app_tagline", "") or ""),
        })

    def _save_llm(self, body: dict):
        pid = str(body.get("provider_id", "")).strip()
        if not pid:
            return self._json({"ok": False, "error": "missing provider_id"}, 400)
        focus = str(body.get("research_focus", "")).strip()
        prompt = normalize_custom_prompt(str(body.get("system_prompt", "")), focus)

        def mutate(c: dict) -> None:
            apply_llm_save(c, pid,
                           set_active=bool(body.get("set_active")),
                           base_url=str(body.get("base_url", "")),
                           model=str(body.get("model", "")),
                           api_key=str(body.get("api_key", "")),
                           clear_key=bool(body.get("clear_key")),
                           system_prompt=prompt,
                           models=body.get("models") if isinstance(body.get("models"), list) else None,
                           fallback_model=str(body.get("fallback_model", "")).strip(),
                           fallback_provider=str(body.get("fallback_provider", "")).strip())
            if focus:
                c["research_focus"] = focus
        update_config_file(self.config_path, mutate)
        return self._json({"ok": True})

    def _prompt_default(self, body: dict):
        return self._json({"ok": True,
                           "text": default_system_prompt(str(body.get("focus", "")))})

    def _fetch_models(self, body: dict):
        from .llm_providers import fetch_models
        cfg = load_config(self.config_path)
        providers = all_providers(cfg.llm)
        pid = str(body.get("provider_id", "")).strip() or active_id(cfg.llm)
        p = providers.get(pid)
        if not p:
            return self._json({"ok": False, "msg": f"未知服务商 {pid}"}, 404)
        base_url = str(body.get("base_url", "")).strip() or p["base_url"]
        api_key = str(body.get("api_key", "")).strip() or p["api_key"]
        try:
            models = fetch_models(base_url, api_key)
            return self._json({"ok": True, "models": models})
        except Exception as e:  # noqa: BLE001
            return self._json({"ok": False, "msg": str(e)[:150]})

    def _test_llm(self, body: dict):
        cfg = load_config(self.config_path)
        providers = all_providers(cfg.llm)
        pid = str(body.get("provider_id", "")).strip() or active_id(cfg.llm)
        p = providers.get(pid)
        if not p:
            return self._json({"ok": False, "msg": f"未知服务商 {pid}"}, 404)
        draft = {
            "base_url": str(body.get("base_url", "")).strip() or p["base_url"],
            "model": str(body.get("model", "")).strip() or p["model"],
            "api_key": str(body.get("api_key", "")).strip() or p["api_key"],
        }
        ok, msg = test_connection(draft)
        return self._json({"ok": ok, "msg": msg})

    def _save_push(self, body: dict):
        provider = str(body.get("provider", "serverchan")).strip()
        update_config_file(self.config_path, lambda c: apply_push_save(
            c, provider, str(body.get("key", ""))))
        return self._json({"ok": True})

    def _save_sources(self, body: dict):
        updates = {f: str(body.get(f, "")).strip()
                   for f in ("openalex_api_key", "elsevier_api_key",
                             "openalex_email", "crossref_email")
                   if str(body.get(f, "")).strip()}
        if updates:
            update_config_file(self.config_path, lambda c: c.update(updates))
        return self._json({"ok": True, "updated": len(updates)})

    def _save_rank(self, body: dict):
        update_config_file(self.config_path, lambda c: journal_rank.apply_rank_save(
            c, str(body.get("provider", "")), str(body.get("key", ""))))
        return self._json({"ok": True})

    def _test_rank(self, body: dict):
        cfg = load_config(self.config_path)
        ok, msg = journal_rank.test_connection(
            cfg.raw, provider=str(body.get("provider", "")).strip() or None,
            key=str(body.get("key", "")).strip() or None)
        return self._json({"ok": ok, "msg": msg})

    def _save_translate(self, body: dict):
        fields = ("deepl_api_key", "baidu_appid", "baidu_key",
                  "youdao_appkey", "youdao_secret", "mymemory_email")
        updates = {f: str(body.get(f, "")).strip() for f in fields
                   if str(body.get(f, "")).strip()}

        def mutate(c: dict) -> None:
            t = c.setdefault("translate", {})
            engine = str(body.get("engine", "")).strip()
            if engine in ENGINES:
                t["engine"] = engine
            t.update(updates)
        update_config_file(self.config_path, mutate)
        return self._json({"ok": True})

    def _test_translate(self):
        cfg = load_config(self.config_path)
        sample = ("Tool use and reasoning help large language model agents "
                  "plan multi-step tasks and evaluate the results.")
        try:
            zh, engine = translate_text(sample, cfg.raw.get("translate", {}), cfg.llm)
            return self._json({"ok": True,
                               "msg": f"[{ENGINES[engine]['name']}] {zh[:60]}…"})
        except Exception as e:  # noqa: BLE001
            return self._json({"ok": False, "msg": str(e)[:150]})

    def _test_push(self):
        cfg = load_config(self.config_path)
        ok, msg = push_wechat(cfg.push, "✅ lit-zaobao 测试消息",
                              "控制台设置页发出的测试消息，看到即通道正常。")
        return self._json({"ok": ok, "msg": msg})

    def _shutdown(self):
        """先响应再退出：无控制台窗口（--noconsole）模式下的唯一退出入口。"""
        self._json({"ok": True, "msg": "正在退出…"})

        def bye():
            try:
                self.server.shutdown()
            finally:
                os._exit(0)

        threading.Timer(0.4, bye).start()


def make_server(cfg: Config, host: str = "127.0.0.1", port: int = 8000,
                config_path: Path | None = None) -> ThreadingHTTPServer:
    handler = type("Handler", (_Handler,),
                   {"cfg": cfg, "config_path": config_path or CONFIG_PATH})
    return ThreadingHTTPServer((host, port), handler)


def find_free_port(start: int = 8000, span: int = 50) -> int:
    """从 start 起找第一个可绑定的端口（8000 被占时自动顺延）。"""
    import socket
    for port in range(start, start + span):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return start


def find_running_instance(start: int = 8000, span: int = 50) -> str | None:
    """探测已在运行的 lit-zaobao 实例，返回其 URL；没有则 None（防重复双击养出僵尸进程）。"""
    import urllib.request
    for port in range(start, start + span):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status",
                                        timeout=0.4) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            if data.get("app") == "lit-zaobao":
                return f"http://127.0.0.1:{port}"
        except Exception:  # noqa: BLE001 - 端口没人/不是本应用都跳过
            continue
    return None


def open_app_window(url: str) -> None:
    """优先用 Edge --app 模式打开（无地址栏，像原生窗口）；没有 Edge 回落默认浏览器。"""
    import os
    import subprocess
    for exe in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
        if os.path.exists(exe):
            subprocess.Popen([exe, f"--app={url}"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
    webbrowser.open(url)


def serve(port: int = 8000, open_browser: bool = False, app_mode: bool = False) -> None:
    if app_mode:
        existing = find_running_instance(port)
        if existing:
            print(f"已在运行: {existing}，直接打开窗口。")
            open_app_window(existing)
            return
        port = find_free_port(port)
    cfg = load_config()
    server = make_server(cfg, port=port)
    url = f"http://127.0.0.1:{port}"
    if app_mode:
        print("=" * 52)
        print("  lit-zaobao 文献早报机器人")
        print(f"  控制台: {url}")
        print("  保持本窗口打开 = 服务运行中；关闭窗口 = 退出")
        print("=" * 52)
        threading.Timer(1.0, lambda: open_app_window(url)).start()
    else:
        print(f"lit-zaobao 控制台: {url}  （Ctrl+C 退出）")
        if open_browser:
            webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
