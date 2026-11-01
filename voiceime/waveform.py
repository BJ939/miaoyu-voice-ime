# -*- coding: utf-8 -*-
"""录音态实时声纹（波形细线）渲染：透明 RGBA overlay，供悬浮球每帧合成。

需求（2026-10-31 用户实机反馈后重做）：
- 录音中麦克风图标消失，变成实时声纹；**全部是纯细线**，不要圆角粗条、不要深色描边、
  不要外发光/投影（幽灵外观的「极淡 1px 投影」是唯一例外，见下）；
- 静音 = 一条横跨整个圆直径的细线（一条笔尖画出的平线）；
- 说话 = 只画竖细线，不再画横底线；最高峰 = 直径的 80%（上下各留 10%）。

实现（2026-10-31 v3）：
- 9 根竖细线（奇数、左右对称、中心线对齐），线宽 = 静音线粗细 = size*1/70
  （70px 下 1px，最小 1px）；横向总跨度≈0.66*size，居中，左右对称，不出圆。
- 竖线最高峰 = 0.80*size（70px 下 56px，上下各留 7px=10%）；高度按钟形 profile
  从中心向两侧递减；level 做 ^0.70 gamma（小声段也明显）；history 提供每根线不同
  滞后的包络值，形成轻微错相（像声波向外扩散），不用随机数，帧间连续不闪烁。
- 颜色按外观自适应（浅色玻璃上白细线几乎看不见，2026-10-31 预览图实测确认）：
  * 浅色玻璃（light_frost / light_strong / clear / clear_ring）：深色细线 (24,28,36) α235；
  * 深色磨砂（dark_frost）：白色细线 (255,255,255) α250；
  * 极简幽灵（ghost）：白线 α250 + 极淡 1px 投影 (10,14,22) α65，偏移 (1,1)。
    实测（5 种桌面背景 WCAG 对比度）：纯白线在白壁纸上对比度仅 1.01（几乎不可见），
    加 1px 投影后白/浅壁纸最低 1.48、中灰 1.97、深/近黑 3.95~6.99；投影 α85 最低
    1.74 但视觉上已像描边/重影（vision 评审），故取 α65：浅色壁纸可辨、深色壁纸仍
    以白线为主，且保持「1px + 1px 极淡投影」的细线感，不是描边。
- 目标尺寸直接画（不超采样、不缩放）：70px 下每条线就是 1 个物理像素、边缘最锐利；
  125% DPI 下运行时球本就是 70 物理像素（config scale 1.0），线宽 1px 且无亚像素模糊。
- 静音判定带极小阈值（QUIET_LEVEL=0.01）：真机实测发现 `display_level` 是指数衰减的
  浮点值，安静时日志显示 0.000、实际仍是 1e-6~1e-3 的正数，导致渲染走「说话分支」画出
  9 个 1px 小点、满直径横线永不出现（2026-10-31 真机 --toggle 录屏取证）。因此把
  level < 1% 一律按静音处理：低于该值映射出的最高竖线也只有 ~3px（视觉上等于没有），
  直接画满直径横细线更符合「没声音就是一条线」的预期。
- 性能：纯 PIL，每帧只画 1 条横线或 9 条竖线（最多 18 次 rectangle），70px 单帧 <0.5ms
  （30fps 路径不变）；不依赖 numpy / 不依赖 glass2，避免渲染器耦合。
"""
from __future__ import annotations

import math

from PIL import Image, ImageDraw

__all__ = [
    "BARS",
    "LINE_WIDTH_RATIO",
    "MAX_HEIGHT_RATIO",
    "SPAN_RATIO",
    "LEVEL_GAMMA",
    "QUIET_LEVEL",
    "DARK_LINE",
    "WHITE_LINE",
    "GHOST_SHADOW",
    "GHOST_SHADOW_OFFSET",
    "line_width",
    "line_style",
    "line_shadow",
    "bar_x_positions",
    "bar_heights",
    "overlay_levels",
    "render_overlay_bars",
    "render_overlay",
    "compose",
]

# 竖线根数：奇数、左右对称、中心线对齐（9 根在 70px 下间距 4~5px，不糊成一片）
BARS = 9
# 线宽（= 静音线粗细）：size*1/70 → 70px 下 1px；最小 1px（窄球也不消失）
LINE_WIDTH_RATIO = 1.0 / 70.0
# 竖线最高峰 = 0.80*size（上下各留 10%）
MAX_HEIGHT_RATIO = 0.80
# 9 根竖线横向总跨度 ≈ 0.66*size，居中对称（离圆边仍有 ~17%*size 余量，不出圆）
SPAN_RATIO = 0.66
# 条高对 level 做 gamma：level<0.3 的小声段也明显起伏（0.15 → 视觉效果约 0.26）
LEVEL_GAMMA = 0.70
# 静音阈值：display_level 是指数衰减的浮点值，安静时也常是 1e-6~1e-3 的正数（真机实测），
# 低于 1% 一律当静音画满直径横细线（1% 映射的最高竖线也只有 ~3px，视觉上等于没有）。
QUIET_LEVEL = 0.01

# 线色（rgb, alpha）：浅色玻璃用深色细线；深色磨砂/幽灵用白色细线
DARK_LINE = ((24, 28, 36), 235)
WHITE_LINE = ((255, 255, 255), 250)
# 幽灵：白线 + 极淡 1px 投影（颜色, alpha）+ 偏移（右下各 1px）；不是描边
GHOST_SHADOW = ((10, 14, 22), 65)
GHOST_SHADOW_OFFSET = (1, 1)

# 各外观归类（同时接受 UI 的 A/E/F 短代码与 glass2 的方案名）
_LIGHT_LOOKS = frozenset(
    ("light_frost", "light_strong", "clear", "clear_ring", "A", "B", "C", "D")
)
_DARK_LOOKS = frozenset(("dark_frost", "F"))
_GHOST_LOOKS = frozenset(("ghost", "E"))


def _clamp01(value: float) -> float:
    if value <= 0.0:
        return 0.0
    if value >= 1.0:
        return 1.0
    return float(value)


def _bell(count: int) -> list[float]:
    """中心 1.0、两端 0.45 的对称钟形 profile（跟手且有苹果声纹的形状）。"""
    if count <= 1:
        return [1.0]
    mid = (count - 1) / 2.0
    return [1.0 - 0.55 * (abs(i - mid) / mid) ** 1.6 for i in range(count)]


def _bump(count: int, seed: int = 0) -> list[float]:
    """对称的确定性微差（±3%）：保留原「不完全机械骨架」的呼吸感，但左右严格镜像。

    只按「离中心距离」取权重（abs(i-mid)），所以第 i 根与第 count-1-i 根系数完全相同。
    """
    if count <= 1:
        return [1.0]
    mid = (count - 1) / 2.0
    return [1.0 + 0.03 * math.sin(abs(i - mid) * 2.1 + seed * 0.37) for i in range(count)]


def _gamma(level: float) -> float:
    """level → 显示用包络：轻度 gamma 抬高小声段（level^0.7），level=0 仍严格为 0。"""
    value = _clamp01(level)
    return 0.0 if value <= 0.0 else value ** LEVEL_GAMMA


def _is_quiet(level: float) -> bool:
    """是否按静音处理：level < QUIET_LEVEL（含浮点极小残留）→ 画满直径横细线。"""
    return float(level) < QUIET_LEVEL


def line_width(size_px: int) -> int:
    """细线宽度（整数像素）= size*1/70，四舍五入，最小 1px（70px → 1px）。"""
    size = max(1, int(size_px))
    return max(1, int(round(size * LINE_WIDTH_RATIO)))


def line_style(look: str = "A") -> tuple[tuple[int, int, int], int]:
    """按外观返回细线 (rgb, alpha)：浅色玻璃深线、深色磨砂白线、幽灵白线（另加投影）。"""
    key = str(look or "").strip()
    if key in _DARK_LOOKS:
        return WHITE_LINE
    if key in _LIGHT_LOOKS:
        return DARK_LINE
    # 未知外观按默认 A（浅色玻璃）处理；幽灵白线由 line_shadow 追加投影
    if key in _GHOST_LOOKS:
        return WHITE_LINE
    return DARK_LINE


def line_shadow(
    look: str = "A",
) -> tuple[tuple[int, int, int], int, int, int] | None:
    """幽灵外观的极淡 1px 投影（rgb, alpha, dx, dy）；其它外观返回 None（无投影）。"""
    key = str(look or "").strip()
    if key in _GHOST_LOOKS:
        return (GHOST_SHADOW[0], GHOST_SHADOW[1], *GHOST_SHADOW_OFFSET)
    return None


def bar_x_positions(size_px: int, count: int = BARS) -> list[int]:
    """竖线左边缘的整数 x 列表：总跨度≈SPAN_RATIO*size、水平居中、左右对称。

    70px/9 根时得到 [12, 18, 23, 29, 34, 40, 46, 51, 57]：
    左右两两之和 = 69（size-1，即关于球心对称），最外两侧各留 12px 余量，不出圆。
    """
    size = max(8, int(size_px))
    width = line_width(size)
    if count <= 1:
        return [int(round((size - width) / 2.0))]
    span = size * SPAN_RATIO
    gap = (span - count * width) / (count - 1)
    x0 = (size - span) / 2.0
    return [int(round(x0 + i * (width + gap))) for i in range(count)]


def overlay_levels(level: float, count: int = BARS, seed: int = 0) -> list[float]:
    """纯函数：把 0..1 的 level 展开成 count 根线的 0..1 系数（钟形 + 对称微差）。

    level < QUIET_LEVEL（视为静音）时返回全 0（UI 据此画满直径横细线）。
    """
    if _is_quiet(level):
        return [0.0] * count
    lvl = _gamma(level)
    profile = _bell(count)
    bump = _bump(count, seed)
    return [_clamp01(lvl * p * b) for p, b in zip(profile, bump)]


def bar_heights(
    level: float,
    size_px: int,
    count: int = BARS,
    history: list[float] | None = None,
    seed: int = 0,
) -> list[float]:
    """返回每根竖线的高度（像素，浮点，含线宽本身）。

    - level < QUIET_LEVEL（静音，含浮点极小残留）→ 全部 = 线宽（70px 下 1px）；
    - level>0 时先做 level^0.70 的 gamma（小声段也明显），再乘钟形 profile；
    - 结果夹在 [线宽, 0.80*size]：最高峰严格 = 直径 80%（上下各留 10%）；
    - history 存在时，每根线用不同滞后的历史包络混合，形成错相波动
      （稳态时左右仍对称，level 突变/起伏时才出现向外扩散的相位差）。
    """
    size = max(8, int(size_px))
    width = float(line_width(size))
    max_h = max(width + 1.0, size * MAX_HEIGHT_RATIO)
    if _is_quiet(level):
        return [width] * count
    lvl = _gamma(level)
    profile = _bell(count)
    bump = _bump(count, seed)
    heights: list[float] = []
    hist = history or []
    for i, p in enumerate(profile):
        lag_value = lvl
        if hist:
            lag = 1 + i * 2  # 每根线看不同时刻：中心看最新，两侧依次更早
            idx = len(hist) - lag
            lag_value = _gamma(hist[idx]) if idx >= 0 else lvl
        eff = 0.65 * lvl + 0.35 * lag_value
        h = width + (max_h - width) * eff * p * bump[i]
        heights.append(min(max_h, max(width, h)))
    return heights


def _draw_bar(
    draw: ImageDraw.ImageDraw,
    left: int,
    top: int,
    right: int,
    bottom: int,
    color: tuple[int, int, int],
    alpha: int,
    shadow: tuple[tuple[int, int, int], int, int, int] | None,
) -> None:
    """画一条细线矩形：幽灵外观先画右下的极淡 1px 投影，再画白线主体。"""
    if shadow is not None:
        shadow_rgb, shadow_alpha, dx, dy = shadow
        draw.rectangle(
            [left + dx, top + dy, right + dx, bottom + dy],
            fill=(*shadow_rgb, shadow_alpha),
        )
    draw.rectangle([left, top, right, bottom], fill=(*color, alpha))


def render_overlay_bars(
    level: float,
    size_px: int,
    state: str = "recording",
    look: str = "A",
    history: list[float] | None = None,
    count: int = BARS,
    seed: int = 0,
) -> tuple[Image.Image, list[float]]:
    """纯函数：画声纹细线，返回 (RGBA overlay, 每根竖线高度像素)。

    - level < QUIET_LEVEL（静音，含浮点极小残留）：只画一条横跨整个圆直径的水平细线；
    - 其余：只画 count 根竖直细线（不再画横底线）。
    - state 参数保留仅为接口兼容（新配色按外观固定，录音态不再叠红色调）。
    """
    size = max(8, int(size_px))
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    color, alpha = line_style(look)
    shadow = line_shadow(look)
    width = line_width(size)
    heights = bar_heights(level, size, count=count, history=history, seed=seed)
    cy = size / 2.0

    if _is_quiet(level):
        # 静音：一条满直径横细线（不要竖线、不要底线之外的东西）
        top = int(round((size - width) / 2.0))
        _draw_bar(draw, 0, top, size - 1, top + width - 1, color, alpha, shadow)
        return img, heights

    # 说话：只画竖细线；高度取整后按几何中心对称放置（不出圆、不超 80%）
    positions = bar_x_positions(size, count=count)
    for x, height in zip(positions, heights):
        height_px = max(width, min(size, int(round(height))))
        top = int(round(cy - height_px / 2.0))
        _draw_bar(draw, x, top, x + width - 1, top + height_px - 1, color, alpha, shadow)
    return img, heights


def render_overlay(
    level: float,
    size_px: int,
    state: str = "recording",
    look: str = "A",
    history: list[float] | None = None,
) -> Image.Image:
    """返回 size_px×size_px 的透明 RGBA 声纹 overlay（可直接 alpha_composite 到底座）。"""
    overlay, _ = render_overlay_bars(level, size_px, state=state, look=look, history=history)
    return overlay


def compose(base: Image.Image, overlay: Image.Image) -> Image.Image:
    """每帧合成：base 复制一次 + alpha_composite overlay（单次 C 调用，~0.1ms@70px）。"""
    out = base.copy()
    out.alpha_composite(overlay)
    return out
