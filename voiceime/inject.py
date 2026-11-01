# -*- coding: utf-8 -*-
"""Windows 文本注入：剪贴板读写 + SendInput 模拟粘贴/逐字输入。

仅使用标准库 ctypes 调用 user32/kernel32，不依赖第三方包。

对外接口（见 docs/DESIGN.md）：
    class InjectionError(Exception)
    get_clipboard_text() -> str | None
    set_clipboard_text(text) -> bool
    insert_text(text, mode="paste", restore_clipboard=True,
                paste_delay_ms=60, restore_delay_ms=350) -> None
"""
from __future__ import annotations

import ctypes
import logging
import time
from ctypes import wintypes

logger = logging.getLogger(__name__)


class InjectionError(Exception):
    """文本注入失败（消息为可读中文）。"""


# ---------------------------------------------------------------------------
# Win32 常量
# ---------------------------------------------------------------------------
CF_TEXT = 1
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

VK_LCONTROL = 0xA2
VK_V = 0x56
VK_RETURN = 0x0D
VK_TAB = 0x09
MAPVK_VK_TO_VSC = 0

# 所有等待都有上限，保证注入路径不会卡死。
CLIPBOARD_OPEN_TIMEOUT_S = 1.0
CLIPBOARD_RETRY_INTERVAL_S = 0.02
CLIPBOARD_SETTLE_S = 0.03
CTRL_V_STEP_DELAY_S = 0.008
TYPE_CHAR_DELAY_S = 0.002


# ---------------------------------------------------------------------------
# ctypes 结构体：INPUT 联合体在 64 位下必须是 40 字节
# ---------------------------------------------------------------------------
class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [
        ("ki", KEYBDINPUT),
        ("mi", MOUSEINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [
        ("type", wintypes.DWORD),
        ("u", _INPUTUNION),
    ]


# 64 位要求 40 字节；32 位为 28 字节。若不一致说明结构体对齐写错，宁可启动即报错。
_EXPECTED_INPUT_SIZE = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
if ctypes.sizeof(INPUT) != _EXPECTED_INPUT_SIZE:  # pragma: no cover - 环境自检
    raise RuntimeError(
        f"INPUT 结构体大小异常：实际 {ctypes.sizeof(INPUT)} 字节，"
        f"应为 {_EXPECTED_INPUT_SIZE} 字节（请检查 ctypes 结构体对齐）。"
    )


# ---------------------------------------------------------------------------
# Win32 函数签名（显式声明 argtypes/restype，避免 64 位指针截断）
# ---------------------------------------------------------------------------
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.CloseClipboard.argtypes = []
user32.CloseClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.argtypes = []
user32.EmptyClipboard.restype = wintypes.BOOL
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
user32.MapVirtualKeyW.restype = wintypes.UINT

kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = wintypes.LPVOID
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalUnlock.restype = wintypes.BOOL
kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalFree.restype = wintypes.HGLOBAL


def _last_error_text() -> str:
    code = ctypes.get_last_error()
    if not code:
        return "无系统错误码"
    return f"Win32 错误码 {code}"


# ---------------------------------------------------------------------------
# 剪贴板
# ---------------------------------------------------------------------------
def _open_clipboard() -> bool:
    """带超时重试地打开剪贴板（其他程序可能短暂占用）。"""
    deadline = time.monotonic() + CLIPBOARD_OPEN_TIMEOUT_S
    while True:
        if user32.OpenClipboard(None):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(CLIPBOARD_RETRY_INTERVAL_S)


def _read_clipboard_text_ex() -> tuple[bool, str | None]:
    """读取剪贴板文本。

    返回 (是否有文本格式, 文本内容)。读取失败时返回 (False, None)，
    调用方据此决定是否做恢复（宁可不恢复，也不误删用户剪贴板）。
    """
    if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT) and not user32.IsClipboardFormatAvailable(CF_TEXT):
        return (False, None)
    if not _open_clipboard():
        logger.warning("打开剪贴板失败，无法备份原剪贴板内容")
        return (False, None)
    try:
        if user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return (True, "")
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                logger.warning("锁定剪贴板文本内存失败")
                return (True, "")
            try:
                return (True, ctypes.wstring_at(ptr))
            finally:
                kernel32.GlobalUnlock(handle)
        # 仅有 ANSI 文本时按系统代码页（中文 Windows = GBK）解码
        handle = user32.GetClipboardData(CF_TEXT)
        if not handle:
            return (True, "")
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return (True, "")
        try:
            raw = ctypes.string_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
        try:
            return (True, raw.decode("mbcs", errors="replace"))
        except (LookupError, UnicodeError):
            return (True, raw.decode("utf-8", errors="replace"))
    finally:
        user32.CloseClipboard()


def get_clipboard_text() -> str | None:
    """返回剪贴板中的文本；没有文本格式（例如图片）时返回 None。"""
    has_text, text = _read_clipboard_text_ex()
    return text if has_text else None


def set_clipboard_text(text: str) -> bool:
    """把文本写入剪贴板；成功返回 True，失败返回 False（不抛异常）。"""
    data = ("" if text is None else str(text)).encode("utf-16-le") + b"\x00\x00"
    if not _open_clipboard():
        logger.warning("写入剪贴板失败：剪贴板被其他程序占用")
        return False
    handle = None
    try:
        if not user32.EmptyClipboard():
            logger.warning("清空剪贴板失败：%s", _last_error_text())
            return False
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            logger.warning("分配剪贴板内存失败：%s", _last_error_text())
            return False
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            logger.warning("锁定剪贴板内存失败：%s", _last_error_text())
            return False
        try:
            ctypes.memmove(ptr, data, len(data))
        finally:
            kernel32.GlobalUnlock(handle)
        # SetClipboardData 成功后内存所有权归系统，不能再 GlobalFree。
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            logger.warning("写入剪贴板失败：%s", _last_error_text())
            kernel32.GlobalFree(handle)
            handle = None
            return False
        handle = None
        return True
    finally:
        if handle is not None:
            kernel32.GlobalFree(handle)
        user32.CloseClipboard()


# ---------------------------------------------------------------------------
# SendInput
# ---------------------------------------------------------------------------
def _make_vk_input(vk: int, key_up: bool) -> INPUT:
    scan = int(user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)) & 0xFF
    flags = KEYEVENTF_KEYUP if key_up else 0
    item = INPUT()
    item.type = INPUT_KEYBOARD
    item.ki = KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
    return item


def _make_unicode_input(code_unit: int, key_up: bool) -> INPUT:
    flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if key_up else 0)
    item = INPUT()
    item.type = INPUT_KEYBOARD
    item.ki = KEYBDINPUT(wVk=0, wScan=code_unit, dwFlags=flags, time=0, dwExtraInfo=0)
    return item


def _send_inputs(inputs: list[INPUT]) -> None:
    if not inputs:
        return
    array = (INPUT * len(inputs))(*inputs)
    sent = int(user32.SendInput(len(inputs), array, ctypes.sizeof(INPUT)))
    if sent != len(inputs):
        raise InjectionError(
            f"SendInput 发送键盘事件失败（成功 {sent}/{len(inputs)}，{_last_error_text()}）。"
            "若当前焦点窗口以管理员权限运行，Windows 会拦截普通进程的模拟按键，"
            "请以相同权限运行本程序，或手动切换到普通权限窗口。"
        )


def _send_ctrl_v() -> None:
    """发送 Ctrl+V 组合键（分三步并留少量间隔，兼容对按键时序敏感的编辑器）。"""
    ctrl_down = _make_vk_input(VK_LCONTROL, False)
    v_down = _make_vk_input(VK_V, False)
    v_up = _make_vk_input(VK_V, True)
    ctrl_up = _make_vk_input(VK_LCONTROL, True)
    try:
        _send_inputs([ctrl_down])
        time.sleep(CTRL_V_STEP_DELAY_S)
        _send_inputs([v_down, v_up])
        time.sleep(CTRL_V_STEP_DELAY_S)
    finally:
        try:
            _send_inputs([ctrl_up])
        except InjectionError:  # 已经尽力松开 Ctrl，不覆盖原始错误
            logger.warning("释放 Ctrl 键失败（按键可能残留按下状态）")


def _send_virtual_key(vk: int) -> None:
    _send_inputs([_make_vk_input(vk, False), _make_vk_input(vk, True)])


def _send_unicode_char(char: str) -> None:
    """用 KEYEVENTF_UNICODE 发送单个字符（非 BMP 字符按代理对成组发送）。"""
    units = char.encode("utf-16-le")
    events: list[INPUT] = []
    for offset in range(0, len(units), 2):
        code_unit = int.from_bytes(units[offset:offset + 2], "little")
        events.append(_make_unicode_input(code_unit, False))
        events.append(_make_unicode_input(code_unit, True))
    _send_inputs(events)


# ---------------------------------------------------------------------------
# 对外注入接口
# ---------------------------------------------------------------------------
def _insert_by_paste(text: str, restore_clipboard: bool,
                     paste_delay_ms: int, restore_delay_ms: int) -> None:
    had_text, original_text = _read_clipboard_text_ex()
    if not set_clipboard_text(text):
        raise InjectionError(
            "写入剪贴板失败：剪贴板可能被其他程序占用。请稍后重试，"
            "或把 insert_mode 改为 type 使用逐字输入。"
        )
    # 给目标窗口读取剪贴板留一点时间（部分程序打开剪贴板较慢）。
    time.sleep(CLIPBOARD_SETTLE_S)
    _send_ctrl_v()
    time.sleep(max(int(paste_delay_ms), 0) / 1000.0)
    # 只恢复文本型剪贴板；原内容是图片等非文本格式时不恢复，避免破坏用户数据。
    if restore_clipboard and had_text and original_text is not None:
        time.sleep(max(int(restore_delay_ms), 0) / 1000.0)
        if not set_clipboard_text(original_text):
            logger.warning("恢复原剪贴板文本失败（文本注入本身已成功）")


def _insert_by_type(text: str) -> None:
    sent = 0
    for char in text:
        try:
            if char == "\r":
                # \r\n 与裸 \r 都当作一次回车，避免产生双换行
                continue
            if char == "\n":
                _send_virtual_key(VK_RETURN)
            elif char == "\t":
                _send_virtual_key(VK_TAB)
            else:
                _send_unicode_char(char)
        except InjectionError as exc:
            raise InjectionError(
                f"逐字输入失败（已输入前 {sent} 个字符）：{exc}"
            ) from exc
        sent += 1
        time.sleep(TYPE_CHAR_DELAY_S)


def insert_text(text: str, mode: str = "paste", restore_clipboard: bool = True,
                paste_delay_ms: int = 60, restore_delay_ms: int = 350) -> None:
    """把 text 注入当前焦点窗口。

    mode="paste"：备份剪贴板 → 写入文本 → SendInput Ctrl+V → 等待 → 恢复文本型剪贴板；
    mode="type"：SendInput KEYEVENTF_UNICODE 逐字符输入。
    失败抛 InjectionError（中文提示），所有等待均有上限，不会卡死。
    """
    if text is None:
        raise InjectionError("要注入的文本为 None。")
    text = str(text)
    if text == "":
        return
    normalized = (mode or "paste").strip().lower()
    if normalized not in ("paste", "type"):
        raise InjectionError(f"不支持的文本注入模式：{mode!r}，仅支持 'paste' 或 'type'。")
    logger.debug("开始注入文本：mode=%s, 长度=%d", normalized, len(text))
    if normalized == "paste":
        _insert_by_paste(text, restore_clipboard, paste_delay_ms, restore_delay_ms)
    else:
        _insert_by_type(text)
    logger.debug("文本注入完成：mode=%s", normalized)
