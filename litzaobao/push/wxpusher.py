"""WxPusher 通道：APP 扫码订阅后推送到微信。https://wxpusher.zjiecode.com/
appToken 在后台「应用管理」里，uid 在 APP「我的」页；多个 uid 用逗号分隔。"""
from __future__ import annotations

import json
import re

from ..web import http_post

API_URL = "https://wxpusher.zjiecode.com/api/send/message"
MAX_CONTENT = 39000  # WxPusher content 上限约 40000 字符，留余量
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_-]+$")   # appToken 形如 AT_xxx
_SAFE_UID = re.compile(r"^[A-Za-z0-9_-]+$")


def parse_uids(raw: str) -> list[str]:
    """uid 支持逗号分隔多个（兼容中文逗号/分号）。"""
    for sep in ("，", ";", "；"):
        raw = (raw or "").replace(sep, ",")
    return [u.strip() for u in raw.split(",") if u.strip()]


def build_payload(app_token: str, uids: list[str], title: str, md_text: str,
                  full_url: str = "") -> dict:
    """contentType 3 = markdown；summary 是通知栏摘要。"""
    content = md_text
    if len(md_text) > MAX_CONTENT:
        content = md_text[:MAX_CONTENT]
        content += f"\n\n[📖 完整版]({full_url})" if full_url else "…(已截断)"
    return {"appToken": app_token, "contentType": 3, "summary": title[:90],
            "content": content, "uids": list(uids)}


def push(app_token: str, uids_raw: str, title: str, md_text: str,
         full_url: str = "") -> tuple[bool, str]:
    if not app_token:
        return False, "未配置 wxpusher_app_token（https://wxpusher.zjiecode.com 后台获取）"
    if not _SAFE_TOKEN.fullmatch(app_token):
        return False, "wxpusher_app_token 含非法字符（仅允许字母数字、下划线、连字符）"
    uids = parse_uids(uids_raw)
    if not uids:
        return False, "未配置 wxpusher_uid（APP「我的」页的用户 UID，多个逗号分隔）"
    bad = next((u for u in uids if not _SAFE_UID.fullmatch(u)), None)
    if bad:
        return False, f"wxpusher_uid 含非法字符: {bad[:20]}"
    try:
        resp = json.loads(http_post(API_URL, build_payload(app_token, uids, title, md_text, full_url),
                                     timeout=30))
    except Exception as e:  # noqa: BLE001
        return False, f"请求失败: {e}"
    if resp.get("code") == 200:
        return True, f"WxPusher 推送成功（{len(uids)} 个 UID）"
    return False, f"WxPusher 返回错误: {resp.get('msg', resp)}"
