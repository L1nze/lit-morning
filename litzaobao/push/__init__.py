"""推送调度：多渠道 + 主备互备 + 按渠道分片。

渠道 id 与凭据键（全部平铺在 push 节）：
  serverchan  serverchan_sendkey                Server酱·Turbo
  pushplus    pushplus_token                    PushPlus
  wecom       wecom_key                         企业微信群机器人（超长自动分片）
  wxpusher    wxpusher_app_token + wxpusher_uid WxPusher（uid 逗号分隔多个）
  smtp        smtp_host/port/user/pass/to       邮件（465 走 SSL，其余 STARTTLS）
provider 为主通道，fallback_provider 为备通道：主失败自动切备，首个成功即止。
"""
from __future__ import annotations

from . import pushplus, serverchan, smtp, wecom, wxpusher  # noqa: F401


def _filled(cfg: dict, *fields: str) -> bool:
    """凭据键是否全部非空。"""
    return all(str(cfg.get(f) or "").strip() for f in fields)


# 渠道注册表：id -> {name, ready(凭据配齐?), send(cfg_push, title, md, full_url)}
CHANNELS: dict[str, dict] = {
    "serverchan": {
        "name": "Server酱",
        "ready": lambda c: _filled(c, "serverchan_sendkey"),
        "send": lambda c, t, m, u: serverchan.push(c.get("serverchan_sendkey", ""), t, m, full_url=u),
    },
    "pushplus": {
        "name": "PushPlus",
        "ready": lambda c: _filled(c, "pushplus_token"),
        "send": lambda c, t, m, u: pushplus.push(c.get("pushplus_token", ""), t, m, full_url=u),
    },
    "wecom": {
        "name": "企业微信",
        "ready": lambda c: _filled(c, "wecom_key"),
        "send": lambda c, t, m, u: wecom.push(c.get("wecom_key", ""), t, m, full_url=u),
    },
    "wxpusher": {
        "name": "WxPusher",
        "ready": lambda c: _filled(c, "wxpusher_app_token", "wxpusher_uid"),
        "send": lambda c, t, m, u: wxpusher.push(
            c.get("wxpusher_app_token", ""), c.get("wxpusher_uid", ""), t, m, full_url=u),
    },
    "smtp": {
        "name": "邮件",
        "ready": lambda c: _filled(c, "smtp_host", "smtp_user", "smtp_pass", "smtp_to"),
        "send": lambda c, t, m, u: smtp.push(c, t, m, full_url=u),
    },
}

# apply_push_save 里渠道 -> 单一凭据字段 的映射（smtp 无单一 key，凭据在配置文件里填）
_KEY_FIELD = {"serverchan": "serverchan_sendkey", "pushplus": "pushplus_token",
              "wecom": "wecom_key", "wxpusher": "wxpusher_app_token"}


def channel_name(pid: str) -> str:
    """渠道显示名（未知 id 原样返回）。"""
    ch = CHANNELS.get(pid)
    return str(ch["name"]) if ch else pid


def credentials_ok(pid: str, cfg_push: dict) -> bool:
    """单个渠道凭据是否配齐。"""
    ch = CHANNELS.get(pid)
    return bool(ch) and bool(ch["ready"](cfg_push))


def configured_channels(cfg_push: dict) -> list[str]:
    """主+备里凭据配齐的渠道（保持主先备后顺序，去重）。"""
    out: list[str] = []
    for pid in ((cfg_push.get("provider") or "").lower().strip(),
                (cfg_push.get("fallback_provider") or "").lower().strip()):
        if pid and pid not in out and credentials_ok(pid, cfg_push):
            out.append(pid)
    return out


def apply_push_save(cfgd: dict, provider: str, key: str = "") -> None:
    """推送配置写回（配合 config.update_config_file 使用）。"""
    push = cfgd.setdefault("push", {})
    push["provider"] = provider
    field = _KEY_FIELD.get(provider)
    if key.strip() and field:
        push[field] = key.strip()


def push_configured(cfg_push: dict) -> bool:
    """主或备任一通道凭据配齐（决定未推送时是否记已读）。"""
    return any(credentials_ok(pid, cfg_push)
               for pid in ((cfg_push.get("provider") or "").lower().strip(),
                           (cfg_push.get("fallback_provider") or "").lower().strip()))


def push_one(pid: str, cfg_push: dict, title: str, md_text: str,
             full_url: str = "") -> tuple[bool, str]:
    """走指定渠道推送一次（缺凭据/格式错由各渠道模块自行报错）。"""
    ch = CHANNELS.get(pid)
    if not ch:
        return False, f"未知 provider: {pid}"
    return ch["send"](cfg_push, title, md_text, full_url)


def push_wechat(cfg_push: dict, title: str, md_text: str, full_url: str = "") -> tuple[bool, str]:
    """按 [主通道, 备通道] 顺序推送：去重、跳过 none/未知渠道并记说明，首个成功即返回。

    full_url 是当日日报网页地址：内容因渠道上限被截断/分片时，各渠道在截断点
    追加完整版链接（markdown 渠道）；full_url 为空则维持单渠道原行为。
    全部通道失败时返回 False + 各通道原因汇总。"""
    primary = (cfg_push.get("provider") or "none").lower().strip()
    fallback = (cfg_push.get("fallback_provider") or "").lower().strip()
    order: list[str] = []
    skips: list[str] = []
    for role, pid in (("主通道", primary), ("备通道", fallback)):
        if not pid or pid == "none":
            if role == "主通道" and pid == "none":
                skips.append("主通道 provider=none 已跳过")
            continue
        if pid in order:
            continue  # 去重（备通道与主通道相同）
        if pid not in CHANNELS:
            skips.append(f"{role} {pid}: 未知渠道，已跳过")
            continue
        order.append(pid)
    if not order:
        if primary == "none":
            return True, "推送已关闭(provider=none)"
        return False, "；".join(skips) or "无可用的推送渠道"
    failures: list[str] = []
    for pid in order:
        ok, msg = push_one(pid, cfg_push, title, md_text, full_url)
        if ok:
            if skips:
                msg += "（" + "；".join(skips) + "）"
            return True, msg
        failures.append(f"{pid}: {msg}")
    return False, "；".join(skips + failures)
