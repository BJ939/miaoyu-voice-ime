# -*- coding: utf-8 -*-
"""生成干净的 README 预览图（纯色背景渲染，不含任何桌面/系统界面截图）。

用法::

    python -B scripts/make_previews.py

产物（docs/screenshots/）：

- ``look_variants.png``            A/E/F 三套外观（深色背景，4 倍放大）
- ``look_variants_light.png``      A/E/F 三套外观（浅色背景）
- ``states_A.png``                 A 外观的 idle / recording(声纹) / transcribing 状态
- ``waveform_quiet_vs_speech.png`` 录音声纹：静音=满直径横细线 vs 有声=细线起伏

所有画面都由 ``voiceime.glass2`` / ``voiceime.waveform`` 的渲染代码直接生成（与运行时逐像素一致），
不读取屏幕、不含用户桌面内容，可安全随仓库发布。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from voiceime import glass2, waveform  # noqa: E402

OUT = ROOT / "docs" / "screenshots"

BALL = 70          # 悬浮球逻辑尺寸（px）
ZOOM = 4           # 放大倍数（便于 README 展示）
SCALE = BALL * ZOOM

LOOK_VARIANTS = {"A": "light_frost", "E": "ghost", "F": "dark_frost"}

DARK_BG = (20, 24, 29)
LIGHT_BG = (238, 241, 245)
TEXT_DARK_BG = (214, 220, 228)
TEXT_LIGHT_BG = (52, 60, 70)


def _font(size: int) -> ImageFont.ImageFont:
    """优先用系统字体；找不到时退回 PIL 内置字体（保证脚本在任何 Windows 上都能跑）。"""
    for name in ("arial.ttf", "segoeui.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _ball(look: str, state: str = "idle", level: float | None = None) -> Image.Image:
    """渲染一颗悬浮球（按 look/state 取帧；recording + level 时合成实时声纹）。"""
    variant = LOOK_VARIANTS[look]
    if state == "recording" and level is not None:
        base = glass2.render_base(variant, BALL, state="recording")
        overlay = waveform.render_overlay(level, BALL, state="recording", look=look)
        ball = waveform.compose(base, overlay)
    else:
        frames = glass2.render_frames(variant, BALL, state=state)
        ball = frames[len(frames) // 2]
    return ball.resize((SCALE, SCALE), Image.LANCZOS)


def _sheet(
    items: list[tuple[Image.Image, str, str]],
    bg: tuple[int, int, int],
    fg: tuple[int, int, int],
    path: Path,
    title: str,
) -> None:
    """把若干 (图片, 主标题, 副标题) 拼成一张带标题的对比图。"""
    pad = 36
    gap = 44
    label_h = 58
    title_h = 62
    cols = max(1, len(items))
    width = pad * 2 + cols * SCALE + (cols - 1) * gap
    height = title_h + SCALE + label_h + pad

    img = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(img)
    draw.text((pad, 20), title, fill=fg, font=_font(26))

    y = title_h
    for index, (ball, label, sub) in enumerate(items):
        x = pad + index * (SCALE + gap)
        img.paste(ball, (x, y), ball)
        draw.text((x + 2, y + SCALE + 6), label, fill=fg, font=_font(20))
        if sub:
            draw.text((x + 2, y + SCALE + 32), sub, fill=fg, font=_font(15))

    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    print(f"[OK] {path.relative_to(ROOT)}  {img.size[0]}x{img.size[1]}  {path.stat().st_size} bytes")


def main() -> int:
    print(f"项目目录：{ROOT}")
    print(f"输出目录：{OUT}")
    print("")

    variants = [
        (_ball("A"), "Look A", "light frost"),
        (_ball("E"), "Look E", "ghost"),
        (_ball("F"), "Look F", "dark frost"),
    ]
    _sheet(variants, DARK_BG, TEXT_DARK_BG, OUT / "look_variants.png",
           "voice-ime floating ball - three looks (A / E / F)")
    _sheet(variants, LIGHT_BG, TEXT_LIGHT_BG, OUT / "look_variants_light.png",
           "voice-ime floating ball - three looks on light background")

    states = [
        (_ball("A", "idle"), "idle", "microphone icon"),
        (_ball("A", "recording", 0.62), "recording", "live waveform"),
        (_ball("A", "transcribing"), "transcribing", "amber pulse"),
    ]
    _sheet(states, DARK_BG, TEXT_DARK_BG, OUT / "states_A.png",
           "voice-ime states (look A)")

    waveform_pair = [
        (_ball("A", "recording", 0.0), "silence", "full-diameter thin line"),
        (_ball("A", "recording", 0.85), "speech", "9 thin lines"),
    ]
    _sheet(waveform_pair, DARK_BG, TEXT_DARK_BG, OUT / "waveform_quiet_vs_speech.png",
           "recording waveform: silence vs speech (look A)")

    print("")
    print("完成：4 张预览图已生成（纯渲染，不含屏幕截图）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
