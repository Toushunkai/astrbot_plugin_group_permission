"""一次性跑完本插件的全部测试。

跑法： python3 _tests/run_all.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TESTS = [
    "test_gate.py",
    "test_plugin.py",
    "test_hardening.py",
    "test_dispatch_order.py",
]


def main() -> int:
    failed = []
    for name in TESTS:
        print(f"\n{'=' * 70}\n>>> {name}\n{'=' * 70}")
        code = subprocess.call([sys.executable, str(HERE / name)])
        if code != 0:
            failed.append(name)
    print(f"\n{'=' * 70}")
    if failed:
        print(f"❌ 失败的测试文件：{', '.join(failed)}")
        return 1
    print("✅ 全部测试通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
