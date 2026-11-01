# -*- coding: utf-8 -*-
"""全局热键监听：pynput.keyboard.Listener + 自维护按下键集合做组合键匹配。

对外接口（见 docs/DESIGN.md）：
    class HotkeyError(Exception)
    class HotkeyManager:
        __init__(hotkey="ctrl+alt+space", cancel_key="esc",
                 quit_hotkey="ctrl+alt+q", on_toggle=None, on_cancel=None, on_quit=None)
        start() -> None      # 失败抛 HotkeyError（中文提示）
        stop() -> None
        running -> bool
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from pynput import keyboard

logger = logging.getLogger(__name__)

# 重复触发去抖窗口（秒）
DEBOUNCE_SECONDS = 0.3
# 监听线程就绪等待上限（秒），避免 start() 永久阻塞
START_TIMEOUT_SECONDS = 3.0

MODIFIERS = frozenset({"ctrl", "alt", "shift", "win"})

# 用户书写别名 → 规范名
_ALIASES = {
    "ctrl": "ctrl",
    "control": "ctrl",
    "alt": "alt",
    "option": "alt",
    "shift": "shift",
    "win": "win",
    "windows": "win",
    "super": "win",
    "meta": "win",
    "cmd": "win",
    "command": "win",
    "space": "space",
    "spacebar": "space",
    "esc": "esc",
    "escape": "esc",
    "tab": "tab",
    "enter": "enter",
    "return": "enter",
    "backspace": "backspace",
    "back": "backspace",
    "delete": "delete",
    "del": "delete",
    "insert": "insert",
    "ins": "insert",
    "home": "home",
    "end": "end",
    "pageup": "page_up",
    "page_up": "page_up",
    "pgup": "page_up",
    "pagedown": "page_down",
    "page_down": "page_down",
    "pgdn": "page_down",
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "capslock": "caps_lock",
    "caps_lock": "caps_lock",
    "numlock": "num_lock",
    "num_lock": "num_lock",
    "scrolllock": "scroll_lock",
    "scroll_lock": "scroll_lock",
    "printscreen": "print_screen",
    "print_screen": "print_screen",
    "pause": "pause",
    "menu": "menu",
    "`": "`",
    "-": "-",
    "=": "=",
    "[": "[",
    "]": "]",
    "\\": "\\",
    ";": ";",
    "'": "'",
    ",": ",",
    ".": ".",
    "/": "/",
}

# 虚拟键码 → 规范名（pynput 在部分输入法/修饰键组合下只给 KeyCode，需要兜底）
_VK_NAMES = {
    0x08: "backspace",
    0x09: "tab",
    0x0D: "enter",
    0x13: "pause",
    0x14: "caps_lock",
    0x1B: "esc",
    0x20: "space",
    0x21: "page_up",
    0x22: "page_down",
    0x23: "end",
    0x24: "home",
    0x25: "left",
    0x26: "up",
    0x27: "right",
    0x28: "down",
    0x2C: "print_screen",
    0x2D: "insert",
    0x2E: "delete",
    0x5B: "win",
    0x5C: "win",
    0x90: "num_lock",
    0x91: "scroll_lock",
    0xA0: "shift",
    0xA1: "shift",
    0xA2: "ctrl",
    0xA3: "ctrl",
    0xA4: "alt",
    0xA5: "alt",
}

_SPECIAL_CHARS = {
    " ": "space",
    "\t": "tab",
    "\r": "enter",
    "\n": "enter",
    "\x1b": "esc",
    "\x08": "backspace",
    "\x7f": "delete",
}

_MODIFIER_KEY_NAMES = {
    "ctrl": "ctrl",
    "ctrl_l": "ctrl",
    "ctrl_r": "ctrl",
    "alt": "alt",
    "alt_l": "alt",
    "alt_r": "alt",
    "alt_gr": "alt",
    "shift": "shift",
    "shift_l": "shift",
    "shift_r": "shift",
    "cmd": "win",
    "cmd_l": "win",
    "cmd_r": "win",
}


class HotkeyError(Exception):
    """热键配置或监听失败（消息为可读中文）。"""


def normalize_key_name(token: str) -> str:
    """把用户书写的键名规范化为内部名称；无法识别时抛 HotkeyError。"""
    text = (token or "").strip()
    if not text:
        raise HotkeyError("热键中存在空键名。")
    lowered = text.lower()
    if lowered in _ALIASES:
        return _ALIASES[lowered]
    if len(lowered) == 1:
        return lowered
    if lowered.startswith("f") and lowered[1:].isdigit() and 1 <= int(lowered[1:]) <= 24:
        return f"f{int(lowered[1:])}"
    raise HotkeyError(f"无法识别的按键名：{text!r}（支持 ctrl/alt/shift/win + 字母数字/space/esc/tab 等）。")


def parse_hotkey(spec: str) -> frozenset[str]:
    """解析 'ctrl+alt+space' 形式的组合键，返回规范化键名集合。"""
    if not isinstance(spec, str) or not spec.strip():
        raise HotkeyError(f"热键格式无效：{spec!r}，示例：ctrl+alt+space")
    parts = [p for p in (piece.strip() for piece in spec.split("+")) if p]
    if len(parts) < 1:
        raise HotkeyError(f"热键格式无效：{spec!r}，示例：ctrl+alt+space")
    names = [normalize_key_name(p) for p in parts]
    final_key = names[-1]
    if final_key in MODIFIERS:
        raise HotkeyError(f"热键必须以非修饰键结尾：{spec!r}（例如 ctrl+alt+space）。")
    for name in names[:-1]:
        if name not in MODIFIERS:
            raise HotkeyError(
                f"热键组合只能在开头使用修饰键（ctrl/alt/shift/win）：{spec!r}。"
            )
    return frozenset(names)


def key_to_name(key) -> str | None:
    """把 pynput 的按键对象规范化为内部名称；无法识别返回 None。"""
    if isinstance(key, keyboard.Key):
        name = getattr(key, "name", None)
        if not name:
            return None
        if name in _MODIFIER_KEY_NAMES:
            return _MODIFIER_KEY_NAMES[name]
        return name
    if isinstance(key, keyboard.KeyCode):
        vk = getattr(key, "vk", None)
        if isinstance(vk, int):
            if vk in _VK_NAMES:
                return _VK_NAMES[vk]
            if 0x41 <= vk <= 0x5A:
                return chr(vk).lower()
            if 0x30 <= vk <= 0x39:
                return chr(vk)
            if 0x70 <= vk <= 0x87:
                return f"f{vk - 0x6F}"
        char = getattr(key, "char", None)
        if char:
            if len(char) == 1:
                code = ord(char)
                if 1 <= code <= 26:  # Ctrl+字母时部分输入法会给控制字符
                    return chr(code + 96)
                if char.lower() in _ALIASES:
                    return _ALIASES[char.lower()]
                if char in _SPECIAL_CHARS:
                    return _SPECIAL_CHARS[char]
                return char.lower()
        return None
    return None


class _Binding:
    __slots__ = ("combo", "callback", "label", "last_fired")

    def __init__(self, combo: frozenset[str], callback: Callable[[], None] | None, label: str):
        self.combo = combo
        self.callback = callback
        self.label = label
        self.last_fired = 0.0


class HotkeyManager:
    """全局热键管理器：监听线程内匹配组合键并触发回调（回调必须快速返回）。"""

    def __init__(self, hotkey: str = "ctrl+alt+space", cancel_key: str = "esc",
                 quit_hotkey: str | None = "ctrl+alt+q",
                 on_toggle: Callable[[], None] | None = None,
                 on_cancel: Callable[[], None] | None = None,
                 on_quit: Callable[[], None] | None = None):
        self._bindings: list[_Binding] = []
        self._add_binding(hotkey, on_toggle, "录音切换热键")
        if cancel_key:
            self._add_binding(cancel_key, on_cancel, "取消键")
        if quit_hotkey:
            self._add_binding(quit_hotkey, on_quit, "退出热键")

        seen: dict[frozenset[str], str] = {}
        for binding in self._bindings:
            if binding.combo in seen:
                raise HotkeyError(
                    f"热键冲突：{binding.label}与{seen[binding.combo]}解析为同一个组合键"
                    f"（{'+'.join(sorted(binding.combo))}），请修改配置。"
                )
            seen[binding.combo] = binding.label

        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}
        self._listener: keyboard.Listener | None = None

    # ------------------------------------------------------------------
    def _add_binding(self, spec: str | None, callback: Callable[[], None] | None, label: str) -> None:
        if not spec:
            return
        combo = parse_hotkey(spec)
        self._bindings.append(_Binding(combo, callback, label))

    @property
    def bindings(self) -> list[tuple[frozenset[str], str]]:
        """返回 [(组合键集合, 说明)]，便于日志与自检。"""
        return [(b.combo, b.label) for b in self._bindings]

    # ------------------------------------------------------------------
    def _satisfied(self) -> set[int]:
        pressed = {name for name, count in self._counts.items() if count > 0}
        return {index for index, binding in enumerate(self._bindings) if binding.combo <= pressed}

    def _on_press(self, key) -> None:
        try:
            name = key_to_name(key)
            if name is None:
                return
            to_fire: list[_Binding] = []
            with self._lock:
                before = self._satisfied()
                self._counts[name] = self._counts.get(name, 0) + 1
                after = self._satisfied()
                now = time.monotonic()
                for index in after - before:
                    binding = self._bindings[index]
                    if now - binding.last_fired < DEBOUNCE_SECONDS:
                        logger.debug("热键 %s 触发过于频繁，忽略", binding.label)
                        continue
                    binding.last_fired = now
                    to_fire.append(binding)
            for binding in to_fire:
                self._invoke(binding)
        except Exception:  # 监听线程里绝不能抛异常，否则监听会被终止
            logger.exception("处理按键按下事件失败")

    def _on_release(self, key) -> None:
        try:
            name = key_to_name(key)
            if name is None:
                return
            with self._lock:
                count = self._counts.get(name, 0)
                if count <= 1:
                    self._counts.pop(name, None)
                else:
                    self._counts[name] = count - 1
        except Exception:
            logger.exception("处理按键释放事件失败")

    def _invoke(self, binding: _Binding) -> None:
        if binding.callback is None:
            logger.debug("热键 %s 已触发（未注册回调）", binding.label)
            return
        try:
            binding.callback()
        except Exception:
            logger.exception("热键 %s 的回调执行失败", binding.label)

    # ------------------------------------------------------------------
    def start(self) -> None:
        if self.running:
            return
        try:
            listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
            listener.start()
        except Exception as exc:
            raise HotkeyError(
                f"全局热键监听启动失败：{exc}。请检查安全软件是否拦截了键盘钩子，或改用其他热键。"
            ) from exc
        # pynput 的 running 属性在钩子安装前就为 True，ready 事件也早于 SystemHook 安装，
        # 这里等待 ready 后再留一小段钩子安装余量，避免 start() 刚返回就按下的按键丢失。
        ready = threading.Event()

        def _wait_ready() -> None:
            try:
                listener.wait()
                ready.set()
            except Exception:  # pragma: no cover - 监听线程异常退出时 wait() 会失败
                logger.debug("等待热键监听就绪失败", exc_info=True)

        waiter = threading.Thread(target=_wait_ready, name="voiceime-hotkey-ready", daemon=True)
        waiter.start()
        waiter.join(timeout=START_TIMEOUT_SECONDS)
        if not ready.is_set() or not listener.running:
            try:
                listener.stop()
                listener.join(timeout=1.0)
            except Exception:
                logger.debug("清理未就绪的热键监听线程失败", exc_info=True)
            raise HotkeyError(
                "全局热键监听未能在 3 秒内就绪，可能被安全软件或系统策略拦截（键盘钩子不可用）。"
            )
        time.sleep(0.05)
        if not listener.running:
            raise HotkeyError("全局热键监听启动后立即退出，可能被安全软件拦截（键盘钩子不可用）。")
        self._listener = listener
        with self._lock:
            self._counts.clear()
        logger.info("全局热键监听已启动：%s", self.bindings)

    def stop(self) -> None:
        listener = self._listener
        self._listener = None
        with self._lock:
            self._counts.clear()
        if listener is None:
            return
        try:
            listener.stop()
        finally:
            try:
                listener.join(timeout=1.0)
            except Exception:
                logger.debug("等待热键监听线程退出失败", exc_info=True)
        logger.info("全局热键监听已停止")

    @property
    def running(self) -> bool:
        listener = self._listener
        return listener is not None and bool(listener.running)
