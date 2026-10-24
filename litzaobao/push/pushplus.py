"""PushPlus 备用通道：推送到微信公众号。token: https://www.pushplus.plus/"""
from __future__ import annotations

import json
import re

from ..web import http_post

MAX_CONTENT = 100000  # PushPlus 上限宽裕，但仍做保护
_SAFE_TOKEN = re.compile(r"^[A-za-z0-9]+$")


def push(token: str, title: str, md_text: str, full_url: str = "") -> tuple[bool, str]:
    if not token:
        return False, "未配置 pushplus_token（https://www.pushplus.plus 获取）"
    if not _SAFE_TOKEN.fullmatch(token):
        return False, "pushplus_token 含非法字符（仅允许字母数字），请检查配置"
    if len(md_text) <= MAX_CONTENT:
        content = md_text
    elif full_url:
        content = md_text[:MAX_CONTENT] + f"\n\n[📖 完整版]({full_url})"
    else:
        content = md_text[:MAX_CONTENT] + "…(已截断)"
    try:
        resp = json.loads(http_post("https://www.pushplus.plus/send", {
            "token": token, "title": title[:100],
            "content": content, "template": "markdown",
        }, timeout=30))
    except Exception as e:  # noqa: BLE001
        return False, f"请求失败: {e}"
    if resp.get("code") == 200:
        return True, "PushPlus 推送成功"
    return False, f"PushPlus 返回错误: {resp.get('msg', resp)}"
