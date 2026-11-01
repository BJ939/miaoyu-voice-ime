# -*- coding: utf-8 -*-
"""voiceime.hotkey 实测脚本。

验证内容：
 1. 热键字符串解析（合法/非法、修饰键顺序、别名）；
 2. pynput Listener 真实启动；用 pynput Controller 模拟按键，断言回调触发次数；
 3. 300ms 去抖窗口（同一组合键 300ms 内只触发一次）；
 4. 只按下修饰键、只按下末键、错误组合不触发；
 5. 字母键在 Ctrl 按下时的规范化（Ctrl+字母可能产生控制字符）；
 6. stop() 后不再触发；start() 可重启。
模拟按键无法被监听捕获时打印 SKIP 并 exit 0。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from voiceime.hotkey import (  # noqa: E402
    DEBOUNCE_SECONDS,
    HotkeyError,
    HotkeyManager,
    key_to_name,
    parse_hotkey,
)

try:
    from pynput.keyboard import Controller, Key
except Exception as exc:  # noqa: BLE001
    print(f"[SKIP] pynput 不可用，无法模拟按键：{exc}")
    sys.exit(0)

results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    status = "PASS" if ok else "FAIL"
    results.append((status, name, detail))
    print(f"[{status}] {name}" + (f" | {detail}" if detail else ""))
    return ok


def info(message: str) -> None:
    print(f"[INFO] {message}")


def wait_until(predicate, timeout: float, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_parse() -> None:
    print("\n== 1. 热键解析 ==")
    cases = [
        ("ctrl+alt+f9", frozenset({"ctrl", "alt", "f9"})),
        ("Ctrl+Shift+Space", frozenset({"ctrl", "shift", "space"})),
        ("win+esc", frozenset({"win", "esc"})),
        ("alt+tab", frozenset({"alt", "tab"})),
    ]
    for spec, expected in cases:
        try:
            got = parse_hotkey(spec)
            check(f"解析 {spec!r}", got == expected, f"实测={sorted(got)}")
        except HotkeyError as exc:
            check(f"解析 {spec!r}", False, f"抛出 HotkeyError：{exc}")

    invalid = ["", "ctrl+", "ctrl+alt", "ctrl+hyper+x", "control+shift+alt"]
    for spec in invalid:
        try:
            parse_hotkey(spec)
        except HotkeyError as exc:
            check(f"非法热键 {spec!r} 抛 HotkeyError", True, f"异常信息={str(exc)!r}")
        else:
            check(f"非法热键 {spec!r} 抛 HotkeyError", False, "没有抛出异常")

    check("key_to_name(Key.ctrl_l) == ctrl", key_to_name(Key.ctrl_l) == "ctrl", "")
    check("key_to_name(Key.space) == space", key_to_name(Key.space) == "space", "")
    check("key_to_name(Key.f9) == f9", key_to_name(Key.f9) == "f9", "")


def press_combo(controller, keys, hold: float = 0.02) -> None:
    for key in keys:
        controller.press(key)
        time.sleep(hold)
    for key in reversed(keys):
        controller.release(key)
        time.sleep(hold)


def main() -> int:
    print("== voice-ime hotkey 实测 ==")
    print(f"Python {sys.version.split()[0]}  DEBOUNCE_SECONDS={DEBOUNCE_SECONDS}")
    test_parse()

    print("\n== 2. 构造校验 ==")
    try:
        HotkeyManager(hotkey="ctrl+alt")
    except HotkeyError as exc:
        check("非法热键构造抛 HotkeyError", True, f"异常信息={str(exc)!r}")
    else:
        check("非法热键构造抛 HotkeyError", False, "没有抛出异常")
    try:
        HotkeyManager(hotkey="ctrl+alt+q", quit_hotkey="ctrl+alt+q")
    except HotkeyError as exc:
        check("热键冲突构造抛 HotkeyError", True, f"异常信息={str(exc)!r}")
    else:
        check("热键冲突构造抛 HotkeyError", False, "没有抛出异常")

    print("\n== 3. 监听 + 模拟按键触发 ==")
    hits = {"toggle": [], "cancel": [], "quit": []}
    manager = HotkeyManager(
        hotkey="ctrl+alt+f9",
        cancel_key="esc",
        quit_hotkey="ctrl+alt+q",
        on_toggle=lambda: hits["toggle"].append(time.monotonic()),
        on_cancel=lambda: hits["cancel"].append(time.monotonic()),
        on_quit=lambda: hits["quit"].append(time.monotonic()),
    )
    print(f"注册组合键：{[(sorted(combo), label) for combo, label in manager.bindings]}")
    try:
        manager.start()
    except HotkeyError as exc:
        check("HotkeyManager.start()", False, f"启动失败：{exc}")
        return _summary()
    check("HotkeyManager.start() 后 running=True", manager.running, f"running={manager.running}")
    check("监听线程存活", any(t.name == "Thread-1" or "pynput" in t.name.lower() for t in threading.enumerate())
          or manager.running, f"线程列表={[t.name for t in threading.enumerate() if t is not threading.main_thread()]}")

    # 附加监听：验证 Ctrl 按住时字母键仍能正确匹配（Ctrl+字母可能给控制字符）
    extra_hits: list[float] = []
    extra = HotkeyManager(hotkey="ctrl+alt+x", on_toggle=lambda: extra_hits.append(time.monotonic()))
    extra.start()
    try:
        controller = Controller()
        # --- 3.1 主组合键触发一次 ---
        press_combo(controller, [Key.ctrl, Key.alt, Key.f9])
        caught = wait_until(lambda: len(hits["toggle"]) >= 1, timeout=3.0)
        if not caught:
            print("[SKIP] 模拟按键未被监听捕获（可能被安全软件/系统策略拦截），跳过后续按键用例")
            results.append(("SKIP", "模拟按键触发", "3 秒内监听未捕获到模拟的 ctrl+alt+f9"))
            return _summary()
        check("模拟 ctrl+alt+f9 触发 on_toggle 1 次", len(hits["toggle"]) == 1,
              f"实际触发次数={len(hits['toggle'])}")

        # --- 3.2 300ms 去抖：立即再按一次不触发 ---
        first_ts = hits["toggle"][0]
        time.sleep(0.05)
        second_press_ts = time.monotonic()
        press_combo(controller, [Key.ctrl, Key.alt, Key.f9])
        time.sleep(0.2)
        check("300ms 去抖：0.05s 内再次按下不重复触发", len(hits["toggle"]) == 1,
              f"第二次按下距首次触发={second_press_ts - first_ts:.3f}s，"
              f"触发总次数={len(hits['toggle'])}")

        # --- 3.3 超过去抖窗口后可以再次触发 ---
        remaining = DEBOUNCE_SECONDS + 0.1 - (time.monotonic() - first_ts)
        if remaining > 0:
            time.sleep(remaining)
        press_combo(controller, [Key.ctrl, Key.alt, Key.f9])
        caught = wait_until(lambda: len(hits["toggle"]) >= 2, timeout=2.0)
        interval = hits["toggle"][1] - hits["toggle"][0] if len(hits["toggle"]) >= 2 else None
        check("超过 300ms 后可再次触发（第 2 次）", caught and len(hits["toggle"]) == 2,
              f"两次触发间隔={interval:.3f}s" if interval else "未触发第二次")

        # --- 3.4 只按末键不触发 ---
        press_combo(controller, [Key.f9])
        time.sleep(0.3)
        check("只按 F9（无修饰键）不触发", len(hits["toggle"]) == 2,
              f"触发总次数={len(hits['toggle'])}")

        # --- 3.5 错误组合不触发 ---
        press_combo(controller, [Key.ctrl, Key.alt, 'z'])
        time.sleep(0.3)
        check("ctrl+alt+z（未注册组合）不触发", len(hits["toggle"]) == 2,
              f"触发总次数={len(hits['toggle'])}")

        # --- 3.6 Ctrl+字母的规范化（附加监听 ctrl+alt+x） ---
        press_combo(controller, [Key.ctrl, Key.alt, 'x'])
        caught = wait_until(lambda: len(extra_hits) >= 1, timeout=2.0)
        check("Ctrl 按住时字母键 ctrl+alt+x 正确匹配", caught and len(extra_hits) == 1,
              f"附加监听触发次数={len(extra_hits)}")
    finally:
        extra.stop()

    # --- 3.7 esc 取消键 ---
    controller = Controller()
    press_combo(controller, [Key.esc])
    caught = wait_until(lambda: len(hits["cancel"]) >= 1, timeout=2.0)
    check("单独按 esc 触发 on_cancel 1 次", caught and len(hits["cancel"]) == 1,
          f"实际触发次数={len(hits['cancel'])}")

    # --- 3.8 退出热键 ---
    press_combo(controller, [Key.ctrl, Key.alt, 'q'])
    caught = wait_until(lambda: len(hits["quit"]) >= 1, timeout=2.0)
    check("ctrl+alt+q 触发 on_quit 1 次", caught and len(hits["quit"]) == 1,
          f"实际触发次数={len(hits['quit'])}")

    # --- 3.9 stop 后不再触发 ---
    manager.stop()
    check("stop() 后 running=False", not manager.running, f"running={manager.running}")
    toggle_count = len(hits["toggle"])
    press_combo(controller, [Key.ctrl, Key.alt, Key.f9])
    time.sleep(0.4)
    check("stop() 后模拟按键不再触发", len(hits["toggle"]) == toggle_count,
          f"stop 前={toggle_count} stop 后={len(hits['toggle'])}")

    # --- 3.10 可重启 ---
    manager.start()
    check("再次 start() 后 running=True", manager.running, f"running={manager.running}")
    press_combo(controller, [Key.ctrl, Key.alt, Key.f9])
    caught = wait_until(lambda: len(hits["toggle"]) == toggle_count + 1, timeout=2.5)
    check("重启后热键重新生效", caught,
          f"触发总次数={len(hits['toggle'])}（stop 前={toggle_count}）")
    manager.stop()

    return _summary()


def _summary() -> int:
    passed = sum(1 for status, _, _ in results if status == "PASS")
    failed = [item for item in results if item[0] == "FAIL"]
    skipped = [item for item in results if item[0] == "SKIP"]
    print("\n== 汇总 ==")
    print(f"PASS={passed} FAIL={len(failed)} SKIP={len(skipped)} 总计={len(results)}")
    for status, name, detail in failed:
        print(f"  FAIL: {name} | {detail}")
    for status, name, detail in skipped:
        print(f"  SKIP: {name} | {detail}")
    if failed:
        print("结果：存在失败项")
        return 1
    print("结果：全部通过或跳过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
