# -*- coding: utf-8 -*-
"""生成 voice-ime 的 exe 图标：复用悬浮球的 A 风格毛玻璃渲染（透明背景多尺寸）。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from voiceime import glass2  # noqa: E402

OUT = ROOT / "build" / "icon.ico"


def main() -> int:
    sizes = [16, 24, 32, 48, 64, 128, 256]
    frames = [glass2.render_icon(n) for n in sizes]
    frames[-1].save(OUT, format="ICO", sizes=[(n, n) for n in sizes], append_images=frames[:-1])
    print(f"ICON={OUT} sizes={sizes} bytes={OUT.stat().st_size}")
    preview = OUT.with_suffix(".png")
    frames[-1].save(preview)
    print(f"PREVIEW={preview}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
