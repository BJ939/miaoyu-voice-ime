# -*- coding: utf-8 -*-
"""voiceime.inject 实测脚本。

验证内容（全部打印实测数字/文本，不允许静默 PASS）：
 1. 剪贴板 set/get 往返（中文/英文/数字/emoji/多行/空串）；
 2. WinForms TextBox 靶窗口：paste / type 两种模式注入后用 WM_GETTEXT 独立读回；
 3. 记事本（notepad.exe）：用空临时文件启动记事本，先 Ctrl+A/Delete 清空活动文档
    （新记事本会恢复上次会话内容，不清空无法稳定复现断言），再 paste / type 注入读回：
    - 记事本有原生文本控件（旧版 Edit 或 WinUI3 的 RichEditD2DPT）时用 WM_GETTEXT 读回；
    - 没有原生文本控件时，清空剪贴板后 Ctrl+A/Ctrl+C 经剪贴板读回；
 4. 剪贴板恢复语义：restore_clipboard=True 恢复原文本、False 保留注入文本；
 5. InjectionError 中文异常路径。

本脚本会短暂抢占桌面焦点，结束时关闭自己启动的窗口/进程，并恢复用户原剪贴板文本。
无法自动化的项打印 SKIP 后继续，全部通过（或仅 SKIP）时 exit 0。
"""
from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from voiceime import inject  # noqa: E402

# ---------------------------------------------------------------------------
# Win32 常量与函数
# ---------------------------------------------------------------------------
WM_GETTEXTLENGTH = 0x000E
WM_GETTEXT = 0x000D
WM_CLOSE = 0x0010
SW_RESTORE = 9
VK_MENU = 0x12
KEYEVENTF_KEYUP = 0x0002
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
CREATE_NO_WINDOW = 0x08000000

user32 = ctypes.WinDLL("user32", use_last_error=True)
_CB = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

user32.EnumWindows.argtypes = [_CB, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.EnumChildWindows.argtypes = [wintypes.HWND, _CB, wintypes.LPARAM]
user32.EnumChildWindows.restype = wintypes.BOOL
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindow.argtypes = [wintypes.HWND]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t]
user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]
user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.SendMessageW.restype = ctypes.c_ssize_t
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL


def cls_of(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def title_of(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def pid_of(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def iter_top_windows():
    result = []

    def _cb(hwnd, _lparam):
        result.append((int(hwnd), cls_of(hwnd), title_of(hwnd), bool(user32.IsWindowVisible(hwnd))))
        return True

    user32.EnumWindows(_CB(_cb), 0)
    return result


def iter_children(hwnd: int):
    result = []

    def _cb(child, _lparam):
        result.append((int(child), cls_of(child)))
        return True

    user32.EnumChildWindows(hwnd, _CB(_cb), 0)
    return result


def read_control_text(hwnd: int) -> str:
    """用 WM_GETTEXTLENGTH + WM_GETTEXT 读回标准 Win32 文本控件内容。"""
    length = int(user32.SendMessageW(hwnd, WM_GETTEXTLENGTH, 0, 0))
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 2)
    user32.SendMessageW(hwnd, WM_GETTEXT, length + 1, ctypes.addressof(buf))
    return buf.value


def click_screen_point(x: int, y: int) -> None:
    user32.SetCursorPos(x, y)
    time.sleep(0.05)
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def control_center(hwnd: int) -> tuple[int, int]:
    rect = wintypes.RECT()
    if user32.GetClientRect(hwnd, ctypes.byref(rect)):
        point = wintypes.POINT()
        point.x = (rect.left + rect.right) // 2
        point.y = (rect.top + rect.bottom) // 2
        if user32.ClientToScreen(hwnd, ctypes.byref(point)):
            return int(point.x), int(point.y)
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2


def force_foreground(hwnd: int, click_point: tuple[int, int] | None = None,
                     timeout: float = 2.5) -> bool:
    """把目标窗口切到前台；SetForegroundWindow 被前台锁拒绝时用 Alt 键/点击兜底。"""
    user32.ShowWindow(hwnd, SW_RESTORE)
    time.sleep(0.15)
    deadline = time.monotonic() + timeout
    if int(user32.GetForegroundWindow() or 0) == hwnd:
        return True
    user32.keybd_event(VK_MENU, 0, 0, 0)
    user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.2)
    if int(user32.GetForegroundWindow() or 0) == hwnd:
        return True
    if click_point is not None:
        click_screen_point(*click_point)
    while time.monotonic() < deadline:
        if int(user32.GetForegroundWindow() or 0) == hwnd:
            return True
        time.sleep(0.1)
    return int(user32.GetForegroundWindow() or 0) == hwnd


def normalize_text(text: str) -> str:
    """控件读回会把 \\n 规范成 \\r\\n，比较前统一。"""
    return (text or "").replace("\r\n", "\n").replace("\r", "\n")


def wait_for_window(predicate, timeout: float, interval: float = 0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for item in iter_top_windows():
            if predicate(item):
                return item
        time.sleep(interval)
    return None


# ---------------------------------------------------------------------------
# 测试记账
# ---------------------------------------------------------------------------
results: list[tuple[str, str, str]] = []  # (status, name, detail)


def check(name: str, ok: bool, detail: str = "") -> bool:
    status = "PASS" if ok else "FAIL"
    results.append((status, name, detail))
    print(f"[{status}] {name}" + (f" | {detail}" if detail else ""))
    return ok


def skip(name: str, reason: str) -> None:
    results.append(("SKIP", name, reason))
    print(f"[SKIP] {name} | {reason}")


def info(message: str) -> None:
    print(f"[INFO] {message}")


# ---------------------------------------------------------------------------
# 1. 剪贴板往返
# ---------------------------------------------------------------------------
CLIPBOARD_SAMPLES = [
    ("中文+英文+数字", "voice-ime 剪贴板往返测试：今天下午3点开个会，记得带上项目周报。ABC-123"),
    ("emoji(非 BMP)", "语音输入 🎙️ 测试 ✅ done"),
    ("多行文本", "第一行\n第二行\r\n第三行"),
    ("空字符串", ""),
]


def test_clipboard_roundtrip() -> None:
    print("\n== 1. 剪贴板 set/get 往返 ==")
    for name, text in CLIPBOARD_SAMPLES:
        ok_set = inject.set_clipboard_text(text)
        time.sleep(0.05)
        got = inject.get_clipboard_text()
        check(
            f"剪贴板往返[{name}]",
            bool(ok_set) and got == text,
            f"写入成功={ok_set} 写入长度={len(text)} 读回长度={len(got) if got is not None else None} "
            f"读回内容={got!r}",
        )


# ---------------------------------------------------------------------------
# 2. WinForms TextBox 靶窗口（原生 EDIT 控件，可用 WM_GETTEXT 独立读回）
# ---------------------------------------------------------------------------
WINFORMS_TITLE = "voice-ime - inject test target"
WINFORMS_SCRIPT = """Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$form = New-Object System.Windows.Forms.Form
$form.Text = 'voice-ime - inject test target'
$form.Width = 640
$form.Height = 220
$form.StartPosition = 'CenterScreen'
$form.TopMost = $false
$textBox = New-Object System.Windows.Forms.TextBox
$textBox.Multiline = $true
$textBox.Dock = 'Fill'
$textBox.ScrollBars = 'Vertical'
$textBox.AcceptsReturn = $true
$textBox.Font = New-Object System.Drawing.Font('Consolas', 16)
$form.Controls.Add($textBox)
$form.Add_Shown({ $textBox.Focus() | Out-Null })
[System.Windows.Forms.Application]::Run($form)
"""

PASTE_TEXT_A = "语音输入注入测试A：今天下午3点开个会，记得带上项目周报。"
PASTE_TEXT_B = "追加文本B ABC-456"
TYPE_TEXT_C = "逐字输入C，含换行\n第二行"


def run_winforms_target() -> bool:
    print("\n== 2. WinForms TextBox 靶窗口（WM_GETTEXT 读回） ==")
    script_path = Path(tempfile.gettempdir()) / "voiceime_inject_target.ps1"
    script_path.write_text(WINFORMS_SCRIPT, encoding="utf-8")
    shell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not shell.is_file():
        skip("WinForms 靶窗口", f"未找到 powershell.exe：{shell}")
        return True
    proc = subprocess.Popen(
        [str(shell), "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
         "-File", str(script_path)],
        creationflags=CREATE_NO_WINDOW,
    )
    form_hwnd = None
    try:
        found = wait_for_window(
            lambda item: item[3] and item[2] == WINFORMS_TITLE, timeout=20.0
        )
        if found is None:
            skip("WinForms 靶窗口", "20 秒内未出现目标窗口（powershell/WinForms 启动失败）")
            return True
        form_hwnd = found[0]
        info(f"靶窗口句柄={form_hwnd} class={found[1]!r} title={found[2]!r} pid={pid_of(form_hwnd)}")
        edits = [h for h, cls in iter_children(form_hwnd) if "edit" in cls.lower()]
        if not edits:
            skip("WinForms 靶窗口", f"未找到 EDIT 子控件，子控件={iter_children(form_hwnd)}")
            return True
        edit_hwnd = edits[0]
        info(f"编辑控件句柄={edit_hwnd} class={cls_of(edit_hwnd)!r}")
        point = control_center(edit_hwnd)
        focused = force_foreground(form_hwnd, point)
        if not check("靶窗口获得前台焦点（避免误注入其他窗口）",
                     focused, f"前台句柄={int(user32.GetForegroundWindow() or 0)} 目标={form_hwnd}"):
            return False
        check("靶窗口初始内容为空", normalize_text(read_control_text(edit_hwnd)) == "",
              f"初始内容={read_control_text(edit_hwnd)!r}")

        marker = "注入前剪贴板标记-winforms-2026"
        inject.set_clipboard_text(marker)
        time.sleep(0.05)
        t0 = time.monotonic()
        inject.insert_text(PASTE_TEXT_A, mode="paste")
        elapsed = time.monotonic() - t0
        time.sleep(0.15)
        got = read_control_text(edit_hwnd)
        check("WinForms paste 注入读回一致",
              normalize_text(got) == PASTE_TEXT_A,
              f"耗时={elapsed:.3f}s 期望={PASTE_TEXT_A!r} 实测={got!r}")
        restored = inject.get_clipboard_text()
        check("WinForms paste 后剪贴板恢复原文本", restored == marker,
              f"期望={marker!r} 实测={restored!r}")

        inject.insert_text(PASTE_TEXT_B, mode="paste", restore_clipboard=False)
        time.sleep(0.15)
        expected = PASTE_TEXT_A + PASTE_TEXT_B
        got = read_control_text(edit_hwnd)
        check("WinForms 第二次 paste（restore_clipboard=False）读回一致",
              normalize_text(got) == expected, f"实测={got!r}")
        left = inject.get_clipboard_text()
        check("WinForms restore_clipboard=False 时剪贴板保留注入文本",
              left == PASTE_TEXT_B, f"期望={PASTE_TEXT_B!r} 实测={left!r}")

        inject.insert_text(TYPE_TEXT_C, mode="type")
        time.sleep(0.2)
        expected = PASTE_TEXT_A + PASTE_TEXT_B + TYPE_TEXT_C
        got = read_control_text(edit_hwnd)
        check("WinForms type 模式逐字输入（含换行）读回一致",
              normalize_text(got) == expected, f"期望={expected!r} 实测={got!r}")

        before_len = len(read_control_text(edit_hwnd))
        inject.insert_text("", mode="paste")
        time.sleep(0.1)
        check("空文本注入为空操作", len(read_control_text(edit_hwnd)) == before_len,
              f"注入前后长度={before_len}")
        return True
    finally:
        if form_hwnd is not None:
            user32.PostMessageW(form_hwnd, WM_CLOSE, 0, 0)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and proc.poll() is None:
            time.sleep(0.1)
        if proc.poll() is None:
            proc.kill()
            info("WinForms 靶进程未在 5 秒内退出，已强制结束")
        else:
            info(f"WinForms 靶进程已退出，exit={proc.returncode}")
        try:
            script_path.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 3. 记事本
# ---------------------------------------------------------------------------
NOTEPAD_PASTE_TEXT = "记事本注入测试：明天上午10点复盘会，请准备周报。"
NOTEPAD_TYPE_TEXT = "追加type文本123"


def send_ctrl_key(letter: str) -> None:
    from pynput.keyboard import Controller, Key

    controller = Controller()
    controller.press(Key.ctrl)
    time.sleep(0.02)
    controller.press(letter)
    controller.release(letter)
    time.sleep(0.02)
    controller.release(Key.ctrl)


def copy_all_readback() -> str:
    """清空剪贴板后 Ctrl+A / Ctrl+C，从剪贴板读回当前焦点窗口文本。

    Win11 WinUI3 记事本没有可用 WM_GETTEXT 的原生文本控件时使用这条路读回。
    """
    inject.set_clipboard_text("")
    time.sleep(0.05)
    send_ctrl_key("a")
    time.sleep(0.15)
    send_ctrl_key("c")
    time.sleep(0.4)
    return inject.get_clipboard_text() or ""


def clear_focused_document() -> None:
    """Ctrl+A + Delete 清空当前焦点文档。

    新记事本会恢复上次会话的标签页内容，必须在注入前清空活动文档，
    否则读回文本会混入历史内容、断言无法稳定复现。
    """
    from pynput.keyboard import Controller, Key

    controller = Controller()
    controller.press(Key.ctrl)
    time.sleep(0.02)
    controller.press("a")
    controller.release("a")
    time.sleep(0.02)
    controller.release(Key.ctrl)
    time.sleep(0.12)
    controller.press(Key.delete)
    controller.release(Key.delete)
    time.sleep(0.15)


def wait_native_text_controls(hwnd: int, timeout: float = 3.0):
    """等待记事本的原生文本控件出现（旧版 Edit / WinUI3 的 RichEditD2DPT 都算）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        edits = [h for h, cls in iter_children(hwnd)
                 if "edit" in cls.lower() or "richedit" in cls.lower()]
        if edits:
            return edits
        time.sleep(0.1)
    return []


def read_target_content(native_edits) -> str:
    if native_edits:
        return normalize_text(read_control_text(native_edits[0]))
    return normalize_text(copy_all_readback())


def same_content(got: str, expected: str) -> bool:
    """比较目标窗口读回内容；容忍部分控件/程序在末尾补的换行。"""
    return normalize_text(got).rstrip("\n") == normalize_text(expected).rstrip("\n")


def run_notepad_target() -> bool:
    print("\n== 3. notepad.exe 注入读回 ==")
    existing = {item[0] for item in iter_top_windows()}
    target_file = Path(tempfile.gettempdir()) / f"voiceime_notepad_target_{os.getpid()}.txt"
    target_file.write_text("", encoding="utf-8")
    proc = subprocess.Popen(["notepad.exe", str(target_file)])
    notepad_hwnd = None
    try:
        found = wait_for_window(
            lambda item: item[0] not in existing and item[3] and item[1] == "Notepad",
            timeout=10.0,
        )
        if found is None:
            skip("记事本注入", "10 秒内未出现 class=Notepad 的新窗口（notepad.exe 不可用，"
                              "或新记事本把文件开成了已有窗口的新标签页）")
            return True
        notepad_hwnd = found[0]
        target_pid = pid_of(notepad_hwnd)
        info(f"记事本窗口句柄={notepad_hwnd} title={found[2]!r} pid={target_pid}")

        center = control_center(notepad_hwnd)
        focused = force_foreground(notepad_hwnd, center)
        if not check("记事本获得前台焦点（避免误注入其他窗口）",
                     focused, f"前台句柄={int(user32.GetForegroundWindow() or 0)} 目标={notepad_hwnd}"):
            return False

        # 等原生文本控件加载（WinUI3 记事本同样使用 RichEditD2DPT 承载文本）
        native_edits = wait_native_text_controls(notepad_hwnd, timeout=3.0)
        children = iter_children(notepad_hwnd)
        info(f"子控件类名={[cls for _, cls in children]}")
        if native_edits:
            info(f"原生文本控件={native_edits}（{cls_of(native_edits[0])}），使用 WM_GETTEXT 读回")
        else:
            info("未找到原生文本控件：改用 Ctrl+A/Ctrl+C 经剪贴板读回")

        # 新记事本会恢复上次会话标签页，先清空当前文档再断言，保证可复现
        clear_focused_document()
        time.sleep(0.2)
        initial = read_target_content(native_edits)
        check("记事本活动文档已清空（排除会话恢复内容干扰）", same_content(initial, ""),
              f"清空后内容={initial!r}")

        marker = "注入前剪贴板标记-notepad-2026"
        inject.set_clipboard_text(marker)
        time.sleep(0.05)
        t0 = time.monotonic()
        inject.insert_text(NOTEPAD_PASTE_TEXT, mode="paste")
        elapsed = time.monotonic() - t0
        time.sleep(0.2)
        restored = inject.get_clipboard_text()
        check("记事本 paste 后剪贴板恢复原文本", restored == marker,
              f"期望={marker!r} 实测={restored!r}")
        got = read_target_content(native_edits)
        check("记事本 paste 注入读回一致",
              same_content(got, NOTEPAD_PASTE_TEXT),
              f"耗时={elapsed:.3f}s 读回路径={'WM_GETTEXT' if native_edits else 'Ctrl+A/C'} "
              f"期望={NOTEPAD_PASTE_TEXT!r} 实测={got!r}")

        # type 模式：此时光标位于已粘贴文本末尾，应追加
        inject.insert_text(NOTEPAD_TYPE_TEXT, mode="type")
        time.sleep(0.3)
        expected = NOTEPAD_PASTE_TEXT + NOTEPAD_TYPE_TEXT
        got = read_target_content(native_edits)
        check("记事本 type 模式追加读回一致", same_content(got, expected),
              f"期望={expected!r} 实测={got!r}")
        return True
    finally:
        if notepad_hwnd is not None:
            target_pid = pid_of(notepad_hwnd)
            try:
                os.kill(target_pid, signal.SIGTERM)
                info(f"已终止测试用记事本进程 pid={target_pid}")
            except OSError as exc:
                info(f"终止记事本失败：{exc}")
        try:
            proc.kill()
        except OSError:
            pass
        for _ in range(20):
            try:
                target_file.unlink()
                break
            except OSError:
                time.sleep(0.05)


# ---------------------------------------------------------------------------
# 4. 异常路径
# ---------------------------------------------------------------------------
def test_errors() -> None:
    print("\n== 4. 异常路径（要求中文 InjectionError） ==")
    try:
        inject.insert_text("abc", mode="bad-mode")
    except inject.InjectionError as exc:
        message = str(exc)
        check("非法注入模式抛 InjectionError 且为中文提示",
              "不支持" in message and "paste" in message, f"异常信息={message!r}")
    except Exception as exc:  # noqa: BLE001
        check("非法注入模式抛 InjectionError", False, f"抛出了 {type(exc).__name__}: {exc}")
    else:
        check("非法注入模式抛 InjectionError", False, "没有抛出异常")

    try:
        inject.insert_text(None)
    except inject.InjectionError as exc:
        check("None 文本抛 InjectionError", "None" in str(exc), f"异常信息={str(exc)!r}")
    except Exception as exc:  # noqa: BLE001
        check("None 文本抛 InjectionError", False, f"抛出了 {type(exc).__name__}: {exc}")
    else:
        check("None 文本抛 InjectionError", False, "没有抛出异常")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> int:
    print("== voice-ime inject 实测 ==")
    print(f"Python {sys.version.split()[0]}  platform={sys.platform}  INPUT结构体={ctypes.sizeof(inject.INPUT)} 字节")
    original_clipboard = inject.get_clipboard_text()
    info(f"测试前用户剪贴板文本={original_clipboard!r}")
    try:
        test_clipboard_roundtrip()
        run_winforms_target()
        run_notepad_target()
        test_errors()
    finally:
        if original_clipboard is not None:
            ok = inject.set_clipboard_text(original_clipboard)
            info(f"恢复用户原剪贴板文本：{ok}")
        else:
            info("测试前剪贴板不是文本（或读取失败），未恢复用户剪贴板")

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
