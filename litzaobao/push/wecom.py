"""企业微信群机器人：POST webhook，markdown 消息按 4096 字节上限分片。
key 是 webhook 地址里 ?key= 后面的串（群设置 → 群机器人 → 添加）。
机器人限频 20 条/分钟，超过 _MAX_PARTS 段的内容截断并附完整版链接。"""
from __future__ import annotations

import json
import re

from ..web import http_post

MAX_BYTES = 4096    # 企业微信 markdown content 的 UTF-8 字节硬上限
_BODY_BUDGET = 3800  # 每段正文预算，余量留给标题头/序号/完整版链接
_MAX_PARTS = 10     # 限频保护：最多发 10 段，超出截断
_MAX_TITLE = 40     # 分片标题头里的标题截断长度
_SAFE_KEY = re.compile(r"^[A-Za-z0-9-]+$")   # 防凭据字符改写 URL（SSRF 面）


def _split_body(md_text: str, budget: int) -> list[str]:
    """按行边界切分，使每段 UTF-8 编码后不超过 budget 字节；超长单行按字节硬切。"""
    parts: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for line in md_text.split("\n"):
        n = len(line.encode("utf-8")) + 1  # +1 是行尾换行
        if cur and cur_len + n > budget:
            parts.append("\n".join(cur))
            cur, cur_len = [], 0
        if n > budget:  # 单行超预算（如超长表格行），按字节硬切，残缺字符丢弃
            raw = line.encode("utf-8")
            parts.extend(raw[i:i + budget].decode("utf-8", errors="ignore")
                         for i in range(0, len(raw), budget))
            continue
        cur.append(line)
        cur_len += n
    if cur:
        parts.append("\n".join(cur))
    return parts or [""]


def build_segments(title: str, md_text: str, full_url: str = "") -> tuple[list[str], bool]:
    """切成多条可直接下发的 markdown content；返回 (消息列表, 是否截断)。

    每条形如 `**{title}**（{i}/{n}）\\n\\n{正文}`，保证整体 UTF-8 ≤4096 字节；
    分片或截断时在最后一条末尾追加完整版链接（full_url 非空时）。"""
    head = f"**{title[:_MAX_TITLE]}**"
    link = f"\n\n[📖 完整版]({full_url})" if full_url else ""
    # 序号头与完整版链接都要占字节，统一从正文预算里预留
    budget = _BODY_BUDGET - len(link.encode("utf-8")) - 24
    parts = _split_body(md_text, budget)
    truncated = False
    if len(parts) > _MAX_PARTS:
        parts = parts[:_MAX_PARTS]
        truncated = True
    if not full_url or (len(parts) == 1 and not truncated):
        link = ""  # 未分片未截断：维持原样，不加链接
    msgs = []
    for i, part in enumerate(parts, 1):
        tail = ""
        if i == len(parts):
            tail = link if link else ("\n\n…(内容过长已截断)" if truncated else "")
        msgs.append(f"{head}（{i}/{len(parts)}）\n\n{part}{tail}")
    return msgs, truncated


def push(key: str, title: str, md_text: str, full_url: str = "") -> tuple[bool, str]:
    if not key:
        return False, "未配置 wecom_key（企业微信群机器人 webhook 的 key 参数）"
    if not _SAFE_KEY.fullmatch(key):
        return False, "wecom_key 含非法字符（仅允许字母数字与连字符），请检查配置"
    msgs, truncated = build_segments(title, md_text, full_url)
    for i, content in enumerate(msgs, 1):
        try:
            resp = json.loads(http_post(
                f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={key}",
                {"msgtype": "markdown", "markdown": {"content": content}}, timeout=30))
        except Exception as e:  # noqa: BLE001
            return False, f"第 {i}/{len(msgs)} 段请求失败: {e}"
        if resp.get("errcode") != 0:
            return False, f"企业微信返回错误（第 {i}/{len(msgs)} 段）: {resp.get('errmsg', resp)}"
    if len(msgs) == 1 and not truncated:
        return True, "企业微信推送成功"
    return True, f"企业微信推送成功（{len(msgs)} 段{'，超出部分已截断' if truncated else ''}）"
