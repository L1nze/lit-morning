#!/usr/bin/env python3
"""PyInstaller 打包入口。
不带参数 → 原生 GUI；带子命令（today/models/...）→ 无头 CLI（供计划任务调用）。
无控制台窗口（--noconsole）下异常无处可看，落盘到 exe 旁的 lit-zaobao-error.log。"""

import sys
import traceback
from pathlib import Path

from litzaobao.cli import main

if __name__ == "__main__":
    try:
        main(sys.argv[1:] if len(sys.argv) > 1 else ["gui"])
    except BaseException:  # SystemExit 也要留痕（config 损坏等静默退出场景）
        root = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(".")
        (root / "lit-zaobao-error.log").write_text(traceback.format_exc(), encoding="utf-8")
        raise
