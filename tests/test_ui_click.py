# -*- coding: utf-8 -*-
"""悬浮球鼠标交互回归测试（Lead 维护）：单击开始/结束录音、双击去抖、拖动不误触发。

注意：悬浮球是"面积最大"的那个 TkTopLevel(voice-ime) 窗口；另有一个同名气泡窗口
（较小、不可见/点击穿透），不能用 FindWindow 随便取第一个。
运行：python -B tests/test_ui_click.py
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:  # pragma: no cover
    ctypes.windll.user32.SetProcessDPIAware()

u = ctypes.WinDLL("user32", use_last_error=True)
u.GetWindowRect.argtypes = (wt.HWND, ctypes.POINTER(wt.RECT))
u.GetClassNameW.argtypes = (wt.HWND, wt.LPWSTR, ctypes.c_int)
u.GetWindowTextW.argtypes = (wt.HWND, wt.LPWSTR, ctypes.c_int)

LOG = ROOT / "logs" / "app.log"


def log_text() -> str:
    return LOG.read_text(encoding="utf-8", errors="replace") if LOG.exists() else ""


def tk_windows() -> list[tuple[int, tuple[int, int, int, int]]]:
    result: list[tuple[int, tuple[int, int, int, int]]] = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def _cb(hwnd, _):
        cls = ctypes.create_unicode_buffer(64)
        u.GetClassNameW(hwnd, cls, 64)
        title = ctypes.create_unicode_buffer(64)
        u.GetWindowTextW(hwnd, title, 64)
        if cls.value == "TkTopLevel" and title.value == "voice-ime":
            rect = wt.RECT()
            u.GetWindowRect(hwnd, ctypes.byref(rect))
            result.append((hwnd, (rect.left, rect.top, rect.right, rect.bottom)))
        return True

    u.EnumWindows(_cb, 0)
    return result


def ball() -> tuple[int, tuple[int, int, int, int]] | None:
    windows = tk_windows()
    if not windows:
        return None
    return max(windows, key=lambda w: (w[1][2] - w[1][0]) * (w[1][3] - w[1][1]))


def wait_for(predicate, timeout: float, interval: float = 0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def click(x: int, y: int, count: int = 1) -> None:
    from pynput.mouse import Button, Controller

    mouse = Controller()
    mouse.position = (x, y)
    time.sleep(0.08)
    for _ in range(count):
        mouse.press(Button.left)
        time.sleep(0.03)
        mouse.release(Button.left)
        if count > 1:
            time.sleep(0.07)


def drag(x: int, y: int, dx: int) -> None:
    from pynput.mouse import Button, Controller

    mouse = Controller()
    mouse.position = (x, y)
    time.sleep(0.1)
    mouse.press(Button.left)
    time.sleep(0.1)
    for step in range(1, 11):
        mouse.position = (x - dx * step // 10, y)
        time.sleep(0.02)
    time.sleep(0.1)
    mouse.release(Button.left)


def main() -> int:
    failures = 0
    subprocess.run([PY, "-B", str(ROOT / "app.py"), "--quit"], cwd=str(ROOT), capture_output=True, timeout=30)
    time.sleep(1.5)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text("", encoding="utf-8")
    proc = subprocess.Popen(
        [PY, "-B", str(ROOT / "app.py"), "--console"],
        cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        if not wait_for(lambda: "麦克风常驻采集就绪" in log_text(), 60):
            print("[FAIL] app 未就绪")
            print(log_text()[-2000:])
            return 1
        found = wait_for(ball, 20)
        if not found:
            print("[FAIL] 找不到悬浮球窗口")
            return 1
        _, rect = found
        cx, cy = (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2
        print(f"[BALL] rect={rect} center=({cx},{cy})")

        before = log_text()
        click(cx, cy)
        ok = wait_for(lambda: "state -> recording" in log_text()[len(before):], 8)
        print(f"[{'PASS' if ok else 'FAIL'}] 单击开始录音（进入 recording）")
        failures += 0 if ok else 1

        time.sleep(1.2)
        before = log_text()
        click(cx, cy)
        ok = wait_for(
            lambda: any(k in log_text()[len(before):] for k in ("state -> transcribing", "没听清", "识别完成")),
            10,
        )
        print(f"[{'PASS' if ok else 'FAIL'}] 再次单击结束录音（进入识别/回到 idle）")
        failures += 0 if ok else 1
        idle = wait_for(lambda: "state -> idle" in log_text() or "state -> error" in log_text(), 10)
        print(f"[{'PASS' if idle else 'FAIL'}] 识别流程回到 idle")
        failures += 0 if idle else 1

        time.sleep(0.8)
        before = log_text()
        click(cx, cy, count=2)
        time.sleep(1.6)
        window = log_text()[len(before):]
        starts = window.count("state -> recording")
        stops = window.count("state -> transcribing")
        ok = starts == 1 and stops == 0
        print(f"[{'PASS' if ok else 'FAIL'}] 双击去抖（recording={starts}，transcribing={stops}，期望 1/0）")
        failures += 0 if ok else 1
        click(cx, cy)
        time.sleep(2.5)

        current = ball()
        if not current:
            print("[FAIL] 拖动前找不到悬浮球")
            return 1
        _, rect2 = current
        before = log_text()
        drag((rect2[0] + rect2[2]) // 2, (rect2[1] + rect2[3]) // 2, 120)
        time.sleep(0.8)
        _, rect3 = ball()
        moved = abs(rect3[0] - rect2[0]) > 20
        toggled = "state -> recording" in log_text()[len(before):]
        ok = moved and not toggled
        print(f"[{'PASS' if ok else 'FAIL'}] 拖动仍可用且不误触发录音（x: {rect2[0]} -> {rect3[0]}）")
        failures += 0 if ok else 1
    finally:
        subprocess.run([PY, "-B", str(ROOT / "app.py"), "--quit"], cwd=str(ROOT), capture_output=True, timeout=30)
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()

    print(f"=== 结果：{'全部通过' if failures == 0 else f'{failures} 项失败'} ===")
    if failures:
        print("---- 日志尾部 ----")
        print(log_text()[-3000:])
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
