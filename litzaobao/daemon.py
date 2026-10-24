"""常驻模式时间计算。"""
from __future__ import annotations

import time
from datetime import datetime, timedelta


def _seconds_until(hhmm: str) -> float:
    try:
        hh, mm = map(int, hhmm.split(":"))
    except ValueError:
        raise SystemExit(f"--at 格式应为 HH:MM，收到: {hhmm!r}")
    now = datetime.now()
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def run_daemon(at: str, no_push: bool = False) -> None:
    print(f"daemon 启动：每天 {at} 生成早报，Ctrl+C 退出。")
    while True:
        wait = _seconds_until(at)
        print(f"[{datetime.now():%H:%M:%S}] 下次运行在 {wait / 3600:.1f} 小时后 ...")
        time.sleep(max(wait, 1))
        try:
            from .config import load_config
            from .digest import run_digest
            run_digest(load_config(), no_push=no_push)
        except Exception:  # noqa: BLE001 - 守护进程不能死
            import traceback
            traceback.print_exc()
            print("本轮失败，明天继续。")
