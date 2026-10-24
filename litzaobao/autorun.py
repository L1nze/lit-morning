"""Windows 计划任务管理：exe 内一键开启/关闭每日自动推送（换机器零安装）。

frozen 模式下任务指向 exe 自身（`lit-zaobao.exe today` 无头跑）；
源码模式指向 `python bot.py today`。无需管理员权限（当前用户任务）。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

TASK_NAME = "lit-zaobao-daily"


def _task_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" today'
    bot = Path(__file__).resolve().parent.parent / "bot.py"
    return f'"{sys.executable}" "{bot}" today'


def task_exists() -> bool:
    try:
        r = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME],
                           capture_output=True, timeout=15)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _valid_time(at: str) -> bool:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", at.strip())
    return bool(m and 0 <= int(m.group(1)) <= 23 and 0 <= int(m.group(2)) <= 59)


def register_task(at: str = "08:30") -> tuple[bool, str]:
    if not _valid_time(at):
        return False, f"时间格式应为 HH:MM，收到: {at!r}"
    hh, mm = at.split(":")
    at = f"{int(hh):02d}:{mm}"
    try:
        r = subprocess.run(
            ["schtasks", "/Create", "/TN", TASK_NAME, "/SC", "DAILY",
             "/ST", at, "/F", "/TR", _task_command()],
            capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)[:120]
    if r.returncode == 0:
        return True, f"已开启：每天 {at} 自动运行（本 exe 无头模式）"
    err = (r.stderr or r.stdout).decode("gbk", errors="replace").strip()
    return False, err[:150]


def unregister_task() -> tuple[bool, str]:
    try:
        r = subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
                           capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)[:120]
    return (r.returncode == 0,
            "已关闭每日自动推送" if r.returncode == 0 else "删除失败（任务可能不存在）")
