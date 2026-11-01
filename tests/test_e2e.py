# -*- coding: utf-8 -*-
"""端到端集成测试（Lead 维护）：环境自检 + WAV → 识别 → 结果。

前置：先用 tests/make_fixtures.py 生成 fixtures，或存在 tests/fixtures/zh_short.wav。
运行：python -B tests/test_e2e.py
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
PY = sys.executable


def run(args: list[str], timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PY, "-B", str(ROOT / "app.py")] + args,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def main() -> int:
    failures = 0

    proc = run(["--check"])
    print("=== app.py --check ===")
    print(proc.stdout.strip())
    if proc.returncode != 0:
        print(f"[FAIL] --check 返回 {proc.returncode}")
        failures += 1
    else:
        print("[PASS] --check 全部可用")

    fixture = FIXTURES / "zh_short.wav"
    if not fixture.exists():
        print(f"[SKIP] 缺少测试音频 {fixture}（先运行 tests/make_fixtures.py）")
        return 1 if failures else 0

    proc = run(["--file", str(fixture)])
    print("=== app.py --file zh_short.wav ===")
    print(proc.stdout.strip())
    text = ""
    match = re.search(r"^TEXT=(.*)$", proc.stdout, re.MULTILINE)
    if match:
        text = match.group(1).strip()
    if proc.returncode != 0:
        print(f"[FAIL] --file 返回 {proc.returncode}\n{proc.stderr}")
        failures += 1
    elif "项目周报" not in text or re.search(r"开.{0,2}会", text) is None:
        print(f"[FAIL] 识别文本不符合预期：{text!r}")
        failures += 1
    else:
        print(f"[PASS] 识别文本：{text}")

    print("=== 结果：%s ===" % ("全部通过" if failures == 0 else f"{failures} 项失败"))
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
