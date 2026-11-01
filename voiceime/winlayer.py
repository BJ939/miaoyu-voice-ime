# -*- coding: utf-8 -*-
"""Win32 逐像素 alpha 分层窗口封装（UpdateLayeredWindow + 预乘 BGRA DIB）。

用途：把 Tk 顶层窗口变成「逐像素透明」的分层窗口，让悬浮球真半透明、能透出桌面壁纸；
alpha=0 的像素（球外方形区域）点击穿透，球体半透明区域仍可正常接收鼠标事件。

用法（ui.py）：
    layer = LayeredWindow(top_hwnd)      # top_hwnd = Tk 顶层 wrapper（GetParent(winfo_id())）
    layer.update(image_rgba, x, y)       # image 为 PIL RGBA，x/y 为物理屏幕坐标
    layer.dispose()                      # 退出时释放 GDI 资源

任何一步失败抛 LayeredWindowError（中文可读信息），调用方据此回退色键方案。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

import numpy as np
from PIL import Image

# ---- Win32 常量 ----
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000

ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
BI_RGB = 0
DIB_RGB_COLORS = 0

SRCCOPY = 0x00CC0020

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

HDC = wintypes.HDC
HWND = wintypes.HWND
HBITMAP = wintypes.HBITMAP
HGDIOBJ = wintypes.HGDIOBJ


class LayeredWindowError(RuntimeError):
    """分层窗口初始化/更新失败（调用方可据此回退色键方案）。"""


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte),
        ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte),
        ("AlphaFormat", ctypes.c_ubyte),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


# ---- 显式声明所有 DLL 函数签名（避免 64 位句柄被截断） ----
user32.GetParent.argtypes = (HWND,)
user32.GetParent.restype = HWND
user32.GetWindowLongW.argtypes = (HWND, ctypes.c_int)
user32.GetWindowLongW.restype = ctypes.c_long
user32.SetWindowLongW.argtypes = (HWND, ctypes.c_int, ctypes.c_long)
user32.SetWindowLongW.restype = ctypes.c_long
user32.GetDC.argtypes = (HWND,)
user32.GetDC.restype = HDC
user32.ReleaseDC.argtypes = (HWND, HDC)
user32.ReleaseDC.restype = ctypes.c_int
user32.UpdateLayeredWindow.argtypes = (
    HWND,
    HDC,
    ctypes.POINTER(POINT),
    ctypes.POINTER(SIZE),
    HDC,
    ctypes.POINTER(POINT),
    wintypes.DWORD,
    ctypes.POINTER(BLENDFUNCTION),
    wintypes.DWORD,
)
user32.UpdateLayeredWindow.restype = wintypes.BOOL

gdi32.CreateCompatibleDC.argtypes = (HDC,)
gdi32.CreateCompatibleDC.restype = HDC
gdi32.DeleteDC.argtypes = (HDC,)
gdi32.DeleteDC.restype = wintypes.BOOL
gdi32.CreateDIBSection.argtypes = (
    HDC,
    ctypes.POINTER(BITMAPINFO),
    wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p),
    wintypes.HANDLE,
    wintypes.DWORD,
)
gdi32.CreateDIBSection.restype = HBITMAP
gdi32.SelectObject.argtypes = (HDC, HGDIOBJ)
gdi32.SelectObject.restype = HGDIOBJ
gdi32.DeleteObject.argtypes = (HGDIOBJ,)
gdi32.DeleteObject.restype = wintypes.BOOL


def get_top_hwnd(child_hwnd: int) -> int:
    """Tk 的 winfo_id() 返回 TkChild，真正的顶层窗口是它的父窗口（TkTopLevel）。"""
    hwnd = int(child_hwnd)
    parent = user32.GetParent(hwnd)
    return int(parent) if parent else hwnd


def is_layered(hwnd: int) -> bool:
    """读取 GWL_EXSTYLE，判断 WS_EX_LAYERED 位是否已置位（供自检/证据用）。"""
    style = int(user32.GetWindowLongW(int(hwnd), GWL_EXSTYLE)) & 0xFFFFFFFF
    return bool(style & WS_EX_LAYERED)


def premultiplied_bgra(image: Image.Image) -> bytes:
    """PIL RGBA → UpdateLayeredWindow 需要的预乘 alpha BGRA 字节（top-down 行序）。"""
    if image.mode != "RGBA":
        image = image.convert("RGBA")
    arr = np.asarray(image, dtype=np.uint8)
    alpha = arr[:, :, 3].astype(np.uint16)
    rgb = arr[:, :, :3].astype(np.uint16)
    premul = ((rgb * alpha[:, :, None] + 127) // 255).astype(np.uint8)
    out = np.empty_like(arr)
    out[:, :, 0] = premul[:, :, 2]  # B
    out[:, :, 1] = premul[:, :, 1]  # G
    out[:, :, 2] = premul[:, :, 0]  # R
    out[:, :, 3] = arr[:, :, 3]     # A
    return out.tobytes()


class LayeredWindow:
    """把已有窗口改成 WS_EX_LAYERED，并用 UpdateLayeredWindow 逐帧显示 PIL RGBA 图。"""

    def __init__(self, hwnd: int) -> None:
        self.hwnd = int(hwnd)
        if not self.hwnd:
            raise LayeredWindowError("分层窗口初始化失败：窗口句柄为空")
        self._hdc = None
        self._bitmap = None
        self._old_bitmap = None
        self._bits = ctypes.c_void_p()
        self._size: tuple[int, int] | None = None
        self._last_data: bytes | None = None
        self._last_image = None
        self._disposed = False
        self._make_layered()

    # ------------------------------------------------------------------
    def _make_layered(self) -> None:
        ctypes.set_last_error(0)
        style = int(user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE))
        if style == 0 and ctypes.get_last_error() != 0:
            raise LayeredWindowError(
                f"分层窗口初始化失败：读取窗口样式失败（HWND=0x{self.hwnd:X}，"
                f"错误码 {ctypes.get_last_error()}）"
            )
        new_style = style | WS_EX_LAYERED
        if new_style != style:
            ctypes.set_last_error(0)
            user32.SetWindowLongW(self.hwnd, GWL_EXSTYLE, new_style)
            if ctypes.get_last_error() != 0:
                raise LayeredWindowError(
                    f"分层窗口初始化失败：设置 WS_EX_LAYERED 失败（错误码 "
                    f"{ctypes.get_last_error()}）"
                )
        if not is_layered(self.hwnd):
            raise LayeredWindowError("分层窗口初始化失败：WS_EX_LAYERED 未生效")
        hdc_screen = user32.GetDC(None)
        if not hdc_screen:
            raise LayeredWindowError("分层窗口初始化失败：GetDC 失败")
        try:
            hdc = gdi32.CreateCompatibleDC(hdc_screen)
        finally:
            user32.ReleaseDC(None, hdc_screen)
        if not hdc:
            raise LayeredWindowError("分层窗口初始化失败：CreateCompatibleDC 失败")
        self._hdc = hdc

    # ------------------------------------------------------------------
    def _ensure_dib(self, width: int, height: int) -> None:
        if self._size == (width, height) and self._bitmap:
            return
        self._release_dib()
        bmi = BITMAPINFO()
        header = bmi.bmiHeader
        header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        header.biWidth = width
        header.biHeight = -height  # 负高度 = top-down，行序与 PIL 一致
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = BI_RGB
        header.biSizeImage = width * height * 4
        bits = ctypes.c_void_p()
        ctypes.set_last_error(0)
        bitmap = gdi32.CreateDIBSection(
            self._hdc, ctypes.byref(bmi), DIB_RGB_COLORS, ctypes.byref(bits), None, 0
        )
        if not bitmap:
            raise LayeredWindowError(
                f"创建 DIB 位图失败（{width}x{height}，错误码 {ctypes.get_last_error()}）"
            )
        old = gdi32.SelectObject(self._hdc, bitmap)
        self._bitmap = bitmap
        self._old_bitmap = old
        self._bits = bits
        self._size = (width, height)
        self._last_data = None
        self._last_image = None

    def _release_dib(self) -> None:
        if self._hdc and self._bitmap:
            gdi32.SelectObject(self._hdc, self._old_bitmap)
            gdi32.DeleteObject(self._bitmap)
        self._bitmap = None
        self._old_bitmap = None
        self._bits = ctypes.c_void_p()
        self._size = None
        self._last_data = None
        self._last_image = None

    # ------------------------------------------------------------------
    def update(self, image: Image.Image, x: int, y: int) -> bool:
        """把 RGBA 图显示到窗口 (x, y) 处（物理屏幕坐标）；返回是否成功。"""
        if self._disposed or not self._hdc:
            raise LayeredWindowError("分层窗口已释放，无法更新")
        width, height = image.size
        if width <= 0 or height <= 0:
            raise LayeredWindowError(f"无效的帧尺寸：{width}x{height}")
        self._ensure_dib(width, height)
        if image is self._last_image and self._last_data is not None:
            data = self._last_data
        else:
            data = premultiplied_bgra(image)
            self._last_data = data
            self._last_image = image
        expected = width * height * 4
        if len(data) != expected:
            raise LayeredWindowError(
                f"帧字节数不匹配：期望 {expected}，实际 {len(data)}"
            )
        ctypes.memmove(self._bits, data, expected)

        hdc_screen = user32.GetDC(None)
        if not hdc_screen:
            raise LayeredWindowError("分层窗口更新失败：GetDC 失败")
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        dst = POINT(int(x), int(y))
        size = SIZE(width, height)
        src = POINT(0, 0)
        try:
            ctypes.set_last_error(0)
            ok = user32.UpdateLayeredWindow(
                self.hwnd,
                hdc_screen,
                ctypes.byref(dst),
                ctypes.byref(size),
                self._hdc,
                ctypes.byref(src),
                0,
                ctypes.byref(blend),
                ULW_ALPHA,
            )
        finally:
            user32.ReleaseDC(None, hdc_screen)
        if not ok:
            raise LayeredWindowError(
                f"UpdateLayeredWindow 失败（错误码 {ctypes.get_last_error()}）"
            )
        self._last_x = int(x)
        self._last_y = int(y)
        return True

    def invalidate(self) -> None:
        """丢弃预乘缓存，下一次 update 强制重新转换（帧内容被原地修改时用）。"""
        self._last_image = None
        self._last_data = None

    @property
    def size(self) -> tuple[int, int] | None:
        return self._size

    # ------------------------------------------------------------------
    def dispose(self) -> None:
        """释放 GDI 资源（可重复调用）。"""
        if self._disposed:
            return
        self._disposed = True
        self._release_dib()
        if self._hdc:
            gdi32.DeleteDC(self._hdc)
            self._hdc = None
