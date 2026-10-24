"""Server酱·Turbo 版：推送到微信服务号。免费额度每天 5 条，早报一条刚好。
SendKey: https://sct.ftqq.com/ 登录后复制。"""
from __future__ import annotations

import json
import re

from ..web import http_post

MAX_DESP = 30000  # Server酱正文上限约 32KB，留余量
_SAFE_KEY = re.compile(r"^[A-Za-z0-9]+$")   # 防止凭据里的字符改写 URL（SSRF 面）


def build_payload(sendkey: str, title: str, md_text: str,
                  full_url: str = "") -> tuple[str, dict]:
    """full_url 是当日日报网页地址：正文被截断时附在截断点后，供点开完整版。"""
    url = f"https://sctapi.ftqq.com/{sendkey}.send"
    desp = md_text
    if len(md_text) > MAX_DESP:
        more = f"\n\n[📖 完整版]({full_url})" if full_url else "\n\n…(已截断，完整版见本地 HTML)"
        desp = md_text[:MAX_DESP] + more
    return url, {"title": title[:100], "desp": desp}


def push(sendkey: str, title: str, md_text: str, full_url: str = "") -> tuple[bool, str]:
    if not sendkey:
        return False, "未配置 serverchan_sendkey（https://sct.ftqq.com 获取）"
    if not _SAFE_KEY.fullmatch(sendkey):
        return False, "serverchan_sendkey 含非法字符（仅允许字母数字），请检查配置"
    url, form = build_payload(sendkey, title, md_text, full_url)
    try:
        resp = json.loads(http_post(url, form, form=True, timeout=30))
    except Exception as e:  # noqa: BLE001
        return False, f"请求失败: {e}"
    if resp.get("code") == 0:
        return True, "Server酱推送成功"
    return False, f"Server酱返回错误: {resp.get('message', resp)}"
