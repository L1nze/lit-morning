"""邮件通道：smtplib 发 Markdown 纯文本。
注意：本模块不能叫 email.py，否则会遮蔽标准库 email 包。
端口 465 走 SMTP_SSL，其余端口走 SMTP + STARTTLS；超时 30 秒。"""
from __future__ import annotations

import re
import smtplib
from email.message import EmailMessage

_TIMEOUT = 30
_SSL_PORT = 465
_SAFE_HOST = re.compile(r"^[A-Za-z0-9._-]+$")   # 防止 host 里夹带协议/路径等注入


def _settings(cfg_push: dict) -> tuple[str, int, str, str, list[str]] | str:
    """从 push 节取出 (host, port, user, password, 收件人列表)；缺项/格式错时返回错误说明。"""
    host = str(cfg_push.get("smtp_host") or "").strip()
    port_raw = str(cfg_push.get("smtp_port") or "").strip() or str(_SSL_PORT)
    user = str(cfg_push.get("smtp_user") or "").strip()
    password = str(cfg_push.get("smtp_pass") or "")
    to_raw = str(cfg_push.get("smtp_to") or "").strip()
    if not (host and user and password and to_raw):
        return "未配置完整 SMTP 凭据（smtp_host/smtp_user/smtp_pass/smtp_to）"
    if not _SAFE_HOST.fullmatch(host):
        return "smtp_host 含非法字符（仅允许字母数字、点、下划线、连字符）"
    try:
        port = int(port_raw)
    except ValueError:
        return f"smtp_port 不是合法端口: {port_raw}"
    if not 1 <= port <= 65535:
        return f"smtp_port 超出范围: {port}"
    for sep in ("，", ";", "；"):
        to_raw = to_raw.replace(sep, ",")
    tos = [t.strip() for t in to_raw.split(",") if t.strip()]
    bad = next((t for t in tos if "@" not in t or any(c.isspace() for c in t)), None)
    if bad:
        return f"smtp_to 地址格式可疑: {bad[:30]}"
    return host, port, user, password, tos


def validate(cfg_push: dict) -> tuple[bool, str]:
    """凭据格式校验（不联网、不真发信），供 doctor 自检复用。"""
    got = _settings(cfg_push)
    if isinstance(got, str):
        return False, got
    host, port, user, _, tos = got
    return True, f"SMTP 凭据格式正常（{user}@{host}:{port} → {len(tos)} 个收件人）"


def push(cfg_push: dict, title: str, md_text: str, full_url: str = "") -> tuple[bool, str]:
    got = _settings(cfg_push)
    if isinstance(got, str):
        return False, got
    host, port, user, password, tos = got
    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = user
    msg["To"] = ", ".join(tos)
    msg.set_content(md_text)  # 正文按 Markdown 纯文本发送
    try:
        if port == _SSL_PORT:
            with smtplib.SMTP_SSL(host, port, timeout=_TIMEOUT) as s:
                s.login(user, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=_TIMEOUT) as s:
                s.ehlo()
                s.starttls()
                s.ehlo()
                s.login(user, password)
                s.send_message(msg)
    except Exception as e:  # noqa: BLE001
        return False, f"邮件发送失败: {e}"
    return True, f"邮件已发送（{host}:{port} → {len(tos)} 个收件人）"
