# -*- coding: utf-8 -*-
"""悬浮球视觉候选方案（真 alpha 渲染，可叠加任意桌面背景预览）。

面向需求：高清 / 毛玻璃 / 透明 / 苹果风 / 高级感。
- 所有方案输出 RGBA 真透明，供「逐像素 alpha 分层窗口」显示（能透出桌面）；
- 磨砂感用「半透明白 + 细腻微噪 + 顶部柔光 + 亮边」模拟（系统不做真实背景模糊）；
- 状态用底部小 LED 或细状态环，克制不喧宾夺主。
"""
from __future__ import annotations

import math

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

SS = 6

VARIANTS = ("light_frost", "light_strong", "clear", "clear_ring", "ghost", "dark_frost")

# 每种状态的动画帧数：待机 1、录音 12、识别 16、加载 16、错误 8
FRAME_COUNTS = {
    "idle": 1,
    "recording": 12,
    "transcribing": 16,
    "loading": 16,
    "error": 8,
}

# 静态层缓存：玻璃底/亮边/顶部柔光/麦克风只依赖 (variant, 超采样尺寸, 状态)，
# 逐帧只需要重新叠加底部 LED，可把 53 帧的渲染耗时从 ~2s 降到 ~0.3s。
_STATIC_CACHE: dict[tuple[str, int, str], "Image.Image"] = {}
_STATIC_CACHE_MAX = 6

VARIANT_LABELS = {
    "light_frost": "A 苹果风毛玻璃（推荐）",
    "light_strong": "B 高密度毛玻璃",
    "clear": "C 纯透明玻璃",
    "clear_ring": "D 透明+状态色环",
    "ghost": "E 极简幽灵",
    "dark_frost": "F 深色磨砂",
}

ACCENTS = {
    "idle": (255, 255, 255),
    "recording": (255, 72, 66),
    "transcribing": (255, 170, 50),
    "loading": (70, 160, 255),
    "error": (255, 72, 72),
}


def _mic(size: int, color=(255, 255, 255), alpha: int = 250) -> Image.Image:
    """统一麦克风：实心胶囊 + 半圆托架 + 细支架 + 短底座。"""
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    c = size / 2
    bw = size * 0.190
    bh = size * 0.290
    top = c - size * 0.238
    d.rounded_rectangle([c - bw / 2, top, c + bw / 2, top + bh], radius=bw / 2,
                        fill=(*color, alpha))
    stroke = max(2, int(round(size * 0.032)))
    aw = size * 0.372
    d.arc([c - aw / 2, c - size * 0.078, c + aw / 2, c + size * 0.124],
          start=0, end=180, fill=(*color, alpha), width=stroke)
    d.line([(c, c + size * 0.122), (c, c + size * 0.198)], fill=(*color, alpha), width=stroke)
    dw = size * 0.160
    d.line([(c - dw / 2, c + size * 0.198), (c + dw / 2, c + size * 0.198)],
           fill=(*color, alpha), width=stroke)
    return layer


def _glass_fill(size: int, color: tuple[int, int, int], a_center: int, a_edge: int,
                edge_power: float = 1.6,
                shade_top: int = 10, shade_bottom: int = -14) -> Image.Image:
    """半透明玻璃底：中心更透，边缘略实；上亮下暗的方向光。"""
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
    c = size / 2.0
    nx = (xx - c) / c
    ny = (yy - c) / c
    dist = np.clip(np.sqrt(nx * nx + ny * ny), 0, 1)
    alpha = a_center + (a_edge - a_center) * (dist ** edge_power)
    shade = shade_top * (-ny) + shade_bottom * ny  # 上方 +，下方 -
    rgb = np.array(color, dtype=np.float32)[None, None, :] + shade[:, :, None] * 0.6
    mask = np.clip((1.0 - dist) * size * 0.55, 0, 1)
    arr = np.zeros((size, size, 4), dtype=np.uint8)
    arr[:, :, :3] = np.clip(rgb, 0, 255).astype(np.uint8)
    arr[:, :, 3] = np.clip(alpha * mask, 0, 255).astype(np.uint8)
    img = Image.fromarray(arr, "RGBA")
    # 细腻磨砂：极轻噪声 + 轻微柔化，远看是磨砂、近看干净
    rng = np.random.default_rng(20261027)
    fine = rng.normal(0.0, 2.0, (size, size, 1))
    soft = rng.normal(0.0, 3.0, (size // 14 + 1, size // 14 + 1, 1))
    soft_img = Image.fromarray(
        np.clip(128 + soft[:, :, 0] * 1.6, 0, 255).astype(np.uint8), "L"
    ).resize((size, size), Image.BICUBIC).filter(ImageFilter.GaussianBlur(size * 0.01))
    soft_n = (np.asarray(soft_img, dtype=np.float32) - 128.0)[:, :, None]
    arr = np.asarray(img, dtype=np.float32).copy()
    inner = (arr[:, :, 3:4] > 8).astype(np.float32)
    arr[:, :, :3] = np.clip(arr[:, :, :3] + (fine + soft_n * 0.8) * inner, 0, 255)
    return Image.fromarray(arr.astype(np.uint8), "RGBA")


def _rim(size: int, color=(255, 255, 255), top_alpha: int = 230, bottom_alpha: int = 90,
         width: float = 0.014) -> Image.Image:
    """玻璃边缘亮线：顶部最亮、两端及底部自然衰减。"""
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    steps = 120
    pad = max(1.0, size * 0.005)
    w = max(1, int(round(size * width)))
    for i in range(steps):
        a0 = i * 360.0 / steps
        wt = 0.5 + 0.5 * math.cos(math.radians(a0 - 262))
        a = int(bottom_alpha + (top_alpha - bottom_alpha) * (wt ** 1.7))
        d.arc([pad, pad, size - pad - 1, size - pad - 1],
              start=a0, end=a0 + 360.0 / steps + 0.8, fill=(*color, a), width=w)
    return layer


def _top_light(size: int, alpha: int = 60, width: float = 0.07) -> Image.Image:
    """顶部弧形柔光（模拟玻璃上沿受光）。"""
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    pad = size * 0.015
    d.arc([pad, pad, size - pad, size - pad], start=176, end=356,
          fill=(255, 255, 255, alpha), width=max(2, int(size * width)))
    return layer.filter(ImageFilter.GaussianBlur(size * 0.035))


def _glyph_with_shadow(size: int, color=(255, 255, 255), alpha: int = 252,
                       shadow_alpha: int = 90) -> Image.Image:
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    shadow = _mic(size, (0, 0, 0), shadow_alpha).filter(ImageFilter.GaussianBlur(size * 0.018))
    out.alpha_composite(shadow)
    out.alpha_composite(_mic(size, color, alpha))
    return out


def _led(size: int, accent, phase: float = 0.0) -> Image.Image:
    """底部 LED：小玻璃灯珠 + 柔光。"""
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    cx, cy = size * 0.5, size * 0.785
    breath = 0.5 + 0.5 * math.sin(phase * 2 * math.pi)
    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    rg = size * (0.052 + 0.022 * breath)
    gd.ellipse([cx - rg, cy - rg, cx + rg, cy + rg], fill=(*accent, int(26 + 52 * breath)))
    layer.alpha_composite(glow.filter(ImageFilter.GaussianBlur(size * 0.028)))
    r = size * 0.024
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(*accent, 255))
    d.ellipse([cx - r * 0.55, cy - r * 0.85, cx + r * 0.25, cy - r * 0.2],
              fill=(255, 255, 255, 235))
    return layer


def _status_ring(size: int, accent, width: float = 0.020) -> Image.Image:
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    pad = size * 0.006
    w = max(1, int(round(size * width)))
    d.arc([pad, pad, size - pad - 1, size - pad - 1], start=0, end=360,
          fill=(*accent, 235), width=w)
    return layer.filter(ImageFilter.GaussianBlur(max(1, size * 0.002)))


def _static_base(name: str, s: int, state: str) -> Image.Image:
    """渲染与动画相位无关的静态层（超采样尺寸 s），按 (variant, s, state) 缓存。

    这些层（半透明玻璃底 + 亮边 + 顶部柔光 + 白色麦克风）逐帧不变；
    每帧只需要在副本上叠加底部 LED（相位相关），再缩放回目标尺寸。
    """
    key = (name, s, state)
    cached = _STATIC_CACHE.get(key)
    if cached is not None:
        return cached
    ball = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    colored = state in ("recording", "transcribing", "loading", "error")
    accent = ACCENTS.get(state, ACCENTS["idle"])

    if name == "light_frost":
        ball.alpha_composite(_glass_fill(s, (250, 251, 253), 120, 188))
        ball.alpha_composite(_rim(s, top_alpha=238, bottom_alpha=105, width=0.015))
        ball.alpha_composite(_top_light(s, 62))
        ball.alpha_composite(_glyph_with_shadow(s))
    elif name == "light_strong":
        ball.alpha_composite(_glass_fill(s, (251, 252, 254), 178, 228))
        ball.alpha_composite(_rim(s, top_alpha=244, bottom_alpha=140, width=0.017))
        ball.alpha_composite(_top_light(s, 72))
        ball.alpha_composite(_glyph_with_shadow(s))
    elif name == "clear":
        ball.alpha_composite(_glass_fill(s, (242, 247, 255), 34, 104))
        ball.alpha_composite(_rim(s, top_alpha=222, bottom_alpha=86, width=0.014))
        ball.alpha_composite(_top_light(s, 48))
        ball.alpha_composite(_glyph_with_shadow(s))
    elif name == "clear_ring":
        ball.alpha_composite(_glass_fill(s, (242, 247, 255), 28, 88))
        ring = accent if colored else (255, 255, 255)
        ball.alpha_composite(_status_ring(s, ring, width=0.020 if colored else 0.012))
        ball.alpha_composite(_top_light(s, 40))
        ball.alpha_composite(_glyph_with_shadow(s))
    elif name == "ghost":
        ball.alpha_composite(_glass_fill(s, (246, 249, 255), 12, 36))
        ball.alpha_composite(_rim(s, top_alpha=140, bottom_alpha=45, width=0.010))
        ball.alpha_composite(_glyph_with_shadow(s, alpha=245, shadow_alpha=115))
    else:  # dark_frost
        ball.alpha_composite(_glass_fill(s, (32, 37, 47), 205, 236,
                                          shade_top=8, shade_bottom=-6))
        ball.alpha_composite(_rim(s, top_alpha=156, bottom_alpha=44, width=0.012))
        ball.alpha_composite(_top_light(s, 36))
        ball.alpha_composite(_glyph_with_shadow(s))

    if len(_STATIC_CACHE) >= _STATIC_CACHE_MAX:
        _STATIC_CACHE.pop(next(iter(_STATIC_CACHE)))
    _STATIC_CACHE[key] = ball
    return ball


def _apply_status(ball: Image.Image, name: str, s: int, state: str, phase: float) -> None:
    """在静态层副本上叠加相位相关的状态光效（底部 LED）。"""
    accent = ACCENTS.get(state, ACCENTS["idle"])
    colored = state in ("recording", "transcribing", "loading", "error")
    if name == "ghost":
        # 极简幽灵只在录音/错误时亮灯，其余状态保持干净
        if state in ("recording", "error"):
            ball.alpha_composite(_led(s, accent, phase))
    elif name != "clear_ring" and colored:  # clear_ring 用状态色环代替 LED
        ball.alpha_composite(_led(s, accent, phase))


def render_variant(name: str, size: int = 70, state: str = "idle",
                   phase: float = 0.0) -> Image.Image:
    """返回真 alpha RGBA 图标（size 物理像素）。phase 为 0~1 的动画相位。"""
    name = name if name in VARIANTS else "light_frost"
    size = max(8, int(size))
    s = size * SS
    ball = _static_base(name, s, state).copy()
    _apply_status(ball, name, s, state, float(phase))
    return ball.resize((size, size), Image.LANCZOS)


def render_frames(name: str, size: int = 70, state: str = "idle") -> list[Image.Image]:
    """渲染一个状态的全部动画帧（静态层只算一次，逐帧仅叠加 LED）。"""
    name = name if name in VARIANTS else "light_frost"
    size = max(8, int(size))
    s = size * SS
    count = FRAME_COUNTS.get(state, 1)
    base = _static_base(name, s, state)
    frames: list[Image.Image] = []
    for index in range(count):
        ball = base.copy()
        phase = (index % count) / float(count) if count > 1 else 0.0
        _apply_status(ball, name, s, state, phase)
        frames.append(ball.resize((size, size), Image.LANCZOS))
    return frames


def render_icon(size: int = 256) -> Image.Image:
    """exe 图标：苹果风毛玻璃待机态。"""
    s = int(size) * SS
    ball = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ball.alpha_composite(_glass_fill(s, (250, 251, 253), 150, 210))
    ball.alpha_composite(_rim(s, top_alpha=240, bottom_alpha=120, width=0.012))
    ball.alpha_composite(_top_light(s, 66))
    ball.alpha_composite(_glyph_with_shadow(s))
    return ball.resize((size, size), Image.LANCZOS)


# ---------------------------------------------------------------------------
# 录音态实时声纹底座（无麦克风 glyph，供 waveform overlay 逐帧叠加）
# ---------------------------------------------------------------------------
_BASE_CACHE: dict[tuple[str, int, str], "Image.Image"] = {}
_BASE_CACHE_MAX = 6


def render_base(name: str, size: int = 70, state: str = "recording") -> Image.Image:
    """返回「无麦克风图标」的玻璃底座（真 alpha RGBA），供录音声纹逐帧合成。

    与 ``render_variant`` 的区别：不画麦克风 glyph、不画底部 LED（录音状态由声纹
    本身 + waveform 里的极淡红色调表达），只保留玻璃底/亮边/顶部柔光，
    这样每帧只需 ``alpha_composite`` 一次 waveform overlay 再 UpdateLayeredWindow。
    结果按 (variant, 超采样尺寸, 状态) 缓存：切换外观时只渲染一次（本机实测 29~42ms）。
    """
    name = name if name in VARIANTS else "light_frost"
    size = max(8, int(size))
    s = size * SS
    key = (name, s, state)
    cached = _BASE_CACHE.get(key)
    if cached is None:
        accent = ACCENTS.get(state, ACCENTS["idle"])
        colored = state in ("recording", "transcribing", "loading", "error")
        ball = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        if name == "light_frost":
            ball.alpha_composite(_glass_fill(s, (250, 251, 253), 120, 188))
            ball.alpha_composite(_rim(s, top_alpha=238, bottom_alpha=105, width=0.015))
            ball.alpha_composite(_top_light(s, 62))
        elif name == "light_strong":
            ball.alpha_composite(_glass_fill(s, (251, 252, 254), 178, 228))
            ball.alpha_composite(_rim(s, top_alpha=244, bottom_alpha=140, width=0.017))
            ball.alpha_composite(_top_light(s, 72))
        elif name == "clear":
            ball.alpha_composite(_glass_fill(s, (242, 247, 255), 34, 104))
            ball.alpha_composite(_rim(s, top_alpha=222, bottom_alpha=86, width=0.014))
            ball.alpha_composite(_top_light(s, 48))
        elif name == "clear_ring":
            ball.alpha_composite(_glass_fill(s, (242, 247, 255), 28, 88))
            ring = accent if colored else (255, 255, 255)
            ball.alpha_composite(_status_ring(s, ring, width=0.020 if colored else 0.012))
            ball.alpha_composite(_top_light(s, 40))
        elif name == "ghost":
            ball.alpha_composite(_glass_fill(s, (246, 249, 255), 12, 36))
            ball.alpha_composite(_rim(s, top_alpha=140, bottom_alpha=45, width=0.010))
        else:  # dark_frost
            ball.alpha_composite(_glass_fill(s, (32, 37, 47), 205, 236,
                                             shade_top=8, shade_bottom=-6))
            ball.alpha_composite(_rim(s, top_alpha=156, bottom_alpha=44, width=0.012))
            ball.alpha_composite(_top_light(s, 36))
        if len(_BASE_CACHE) >= _BASE_CACHE_MAX:
            _BASE_CACHE.pop(next(iter(_BASE_CACHE)))
        _BASE_CACHE[key] = ball
        cached = ball
    return cached.resize((size, size), Image.LANCZOS)
