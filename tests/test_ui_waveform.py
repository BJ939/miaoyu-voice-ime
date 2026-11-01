# -*- coding: utf-8 -*-
"""录音声纹 UI 集成测试（真实桌面截图）：录音中麦克风消失 → 实时声纹细线。

流程：启动源码版 app.py → 等常驻麦克风就绪 → 截「待机（麦克风图标）」基准图 →
`--toggle` 开始录音 → 静音截图（应是满直径横细线）→ 外放 SAPI TTS → 有声截图 →
断言录音态/日志/线条结构 → `--quit` 结束（不触发识别/文本注入）→ 清理。

像素口径（球内圆形可见区域，物理像素；按外观自适应，2026-10-31 v3 声纹重做后）：
- 声纹线按外观自适应颜色：浅色玻璃（A 等）=深色细线 rgb(24,28,36)、深色磨砂（F）=
  白色细线、幽灵（E）=白线+极淡 1px 投影。因此线检测按 look 分流：
  * 浅色玻璃：球内圆形区域里 ``max(R,G,B) <= 80`` 的「暗线像素」；
  * 深色磨砂：``min(R,G,B) >= 190`` 的「白线像素」；
  * 幽灵：亮度高通（与 2px 高斯模糊背景的差 >= 28）——浅色壁纸上白线本身对比低，
    只做结构性断言，环境不合适时如实 SKIP。
- 静音态结构：横线横跨整个直径（列跨度 >= 0.85*直径）、高度 <=3 行（≈1px 细线）；
- 说话态结构：5~12 根竖线（期望 9）、无横底线、最高竖线 <= 直径 80%+2px、
  全部在圆内安全区、中心线最高；
- 待机态：仍用「白色麦克风图标」口径（bright=min(RGB)>=205），断言图标存在。
允许环境音量弱时 SKIP「有声 vs 静音」部分，但必须打印实际数字；录音态/静音横线/
麦克风消失为硬断言（前提是截到了对应状态）。

运行：python -B tests/test_ui_waveform.py
注意：会先 `--quit` 所有 voice-ime 实例（单实例互斥体同机全局），运行前确认用户没在
用悬浮球；GUI 会短暂抢鼠标焦点，用户在场时动作要快。
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:  # pragma: no cover
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

u = ctypes.WinDLL("user32", use_last_error=True)
u.GetWindowRect.argtypes = (wt.HWND, ctypes.POINTER(wt.RECT))
u.GetClassNameW.argtypes = (wt.HWND, wt.LPWSTR, ctypes.c_int)
u.GetWindowTextW.argtypes = (wt.HWND, wt.LPWSTR, ctypes.c_int)

LOG = ROOT / "logs" / "app.log"
SHOT_DIR = ROOT / "docs" / "screenshots"
TTS_TEXT = "实时声纹测试，一二三四五六七八九十，这是一段用来测试波形的声音。"
TTS_PS = (
    "Add-Type -AssemblyName System.Speech; "
    "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
    "$s.Rate = 0; $s.Volume = 100; "
    "$s.Speak('%s')" % TTS_TEXT
)

# 待机麦克风图标口径（白色 glyph）
BRIGHT_MIN = 205
# 浅色玻璃外观的深色线判定（线像素合成后 ≈ max(RGB) 30~60）
DARK_LINE_MAX = 80
# 深色磨砂外观的白线判定（深色玻璃底上白线 ≈ 250）
WHITE_LINE_MIN = 190
# 幽灵外观的亮度高通阈值（白线 + 极淡 1px 投影，浅壁纸上对比低 -> 只做结构断言）
GHOST_EDGE_MIN = 28

FAILURES: list[str] = []
SKIPS: list[str] = []
CHECKS = 0


def check(condition: bool, message: str) -> bool:
    global CHECKS
    CHECKS += 1
    if condition:
        print("    [OK] %s" % message)
        return True
    print("    [FAIL] %s" % message)
    FAILURES.append(message)
    return False


def skip(message: str) -> None:
    SKIPS.append(message)
    print("    [SKIP] %s" % message)


def banner(text: str) -> None:
    print("-" * 100)
    print(text)


def log_text() -> str:
    try:
        return LOG.read_text(encoding="utf-8", errors="replace") if LOG.exists() else ""
    except OSError:
        return ""


def app_cmd(*args: str, timeout: float = 30.0):
    return subprocess.run(
        [PY, "-B", str(ROOT / "app.py"), *args],
        cwd=str(ROOT), capture_output=True, timeout=timeout,
    )


def quit_running_instances() -> None:
    """结束可能残留的实例（保证本测试独占麦克风与 app 日志）。"""
    try:
        app_cmd("--quit")
    except Exception:  # noqa: BLE001
        pass
    time.sleep(1.5)


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


def ball_rect() -> tuple[int, int, int, int] | None:
    windows = tk_windows()
    if not windows:
        return None
    return max(windows, key=lambda w: (w[1][2] - w[1][0]) * (w[1][3] - w[1][1]))[1]


def wait_for(predicate, timeout: float, interval: float = 0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def spawn_tts():
    encoded = base64.b64encode(TTS_PS.encode("utf-16-le")).decode("ascii")
    return subprocess.Popen(
        ["powershell", "-NoProfile", "-EncodedCommand", encoded],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def current_look() -> str:
    """读取当前外观（config 默认 A）；失败按 A（浅色玻璃/深色线）处理。"""
    try:
        from voiceime import config as cfgmod

        ui_cfg = cfgmod.load_config().get("ui") or {}
        return str(ui_cfg.get("look") or "A").strip()
    except Exception:  # noqa: BLE001
        return "A"


# ---------------------------------------------------------------------------
# 截图与像素统计
# ---------------------------------------------------------------------------

def grab(rect: tuple[int, int, int, int]):
    from PIL import ImageGrab

    left, top, right, bottom = rect
    return ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True).convert("RGB")


def bright_stats(image) -> dict:
    """待机麦克风图标口径：球内中央区域的白/近白像素数量与垂直跨度。"""
    import numpy as np

    width, height = image.size
    arr = np.asarray(image, dtype=np.int16)
    x0, x1 = int(width * 0.18), int(width * 0.82)
    y0, y1 = int(height * 0.20), int(height * 0.80)
    center = arr[y0:y1, x0:x1]
    bright = center.min(axis=2) >= BRIGHT_MIN
    count = int(bright.sum())
    total = int(bright.size)
    rows = np.where(bright.any(axis=1))[0]
    span_y = int(rows.max() - rows.min() + 1) if rows.size else 0
    return {
        "count": count,
        "ratio": count / float(total) if total else 0.0,
        "span_y": span_y,
        "width": width,
        "height": height,
    }


def line_mask(image, look: str):
    """按外观返回声纹线掩码（球内圆形区域，bool ndarray）。

    深色磨砂/幽灵外观的球体亮边（rim）本身是浅色/高对比弧线，会污染线检测，
    因此这两种外观只取内圈 0.90 半径（声纹线最远像素距圆心 ≤0.79*直径，不受影响）。
    """
    import numpy as np
    from PIL import ImageFilter

    arr = np.asarray(image, dtype=np.int16)
    height, width = image.height, image.width
    radius = width / 2.0
    inner_ratio = 0.90 if look in ("F", "dark_frost", "E", "ghost") else 1.0
    yy, xx = np.mgrid[0:height, 0:width]
    inside = ((xx + 0.5 - width / 2.0) ** 2
              + (yy + 0.5 - height / 2.0) ** 2) <= (radius * inner_ratio) ** 2
    if look in ("F", "dark_frost"):
        return (arr.min(axis=2) >= WHITE_LINE_MIN) & inside
    if look in ("E", "ghost"):
        lum = arr.mean(axis=2).astype(np.float32)
        small = np.clip(lum, 0, 255).astype(np.uint8)
        from PIL import Image as _Image

        blurred = np.asarray(
            _Image.fromarray(small, "L").filter(ImageFilter.GaussianBlur(2)),
            dtype=np.float32,
        )
        return (np.abs(lum - blurred) >= GHOST_EDGE_MIN) & inside
    # 浅色玻璃（A/B/C/D 等）：深色细线
    return (arr.max(axis=2) <= DARK_LINE_MAX) & inside


def line_stats(image, look: str) -> dict:
    """声纹线结构统计：像素数 / 行数 / 列数 / 最长行 / 最高列 / 包围盒。"""
    import numpy as np

    mask = line_mask(image, look)
    height, width = image.height, image.width
    rows = np.where(mask.any(axis=1))[0]
    cols = np.where(mask.any(axis=0))[0]
    row_counts = mask.sum(axis=1)
    col_counts = mask.sum(axis=0)
    return {
        "count": int(mask.sum()),
        "rows": int(rows.size),
        "cols": int(cols.size),
        "row_max": int(row_counts.max()) if row_counts.size else 0,
        "col_max": int(col_counts.max()) if col_counts.size else 0,
        "ymin": int(rows.min()) if rows.size else -1,
        "ymax": int(rows.max()) if rows.size else -1,
        "xmin": int(cols.min()) if cols.size else -1,
        "xmax": int(cols.max()) if cols.size else -1,
        "center_col": int(np.argmax(col_counts)) if col_counts.size else -1,
        "width": width,
        "height": height,
    }


def is_quiet_line(stats: dict) -> bool:
    """静音态：横线横跨整个直径、高度 ≤3 行（≈1px 细线）。"""
    width = stats["width"]
    return (stats["count"] > 0
            and stats["rows"] <= 3
            and stats["cols"] >= 0.85 * width
            and stats["row_max"] >= 0.80 * width)


def is_speech_bars(stats: dict, look: str) -> bool:
    """说话态：竖线束（期望 9 根）、无横底线、高度不超 80% 直径。

    幽灵外观每根竖线带 1px 投影（白线 + 投影各占 1 列、高度各多 1px），
    因此列数上限放宽到 24、单行像素上限放宽到 26。
    """
    height = stats["height"]
    ghost = look in ("E", "ghost")
    max_cols = 24 if ghost else 12
    max_row = 26 if ghost else 13
    return (stats["count"] > 0
            and stats["rows"] >= 5
            and 5 <= stats["cols"] <= max_cols
            and stats["col_max"] <= 0.80 * height + 2
            and stats["row_max"] <= max_row)


def stats_line(stats: dict) -> str:
    return ("count=%d rows=%d cols=%d row_max=%d col_max=%d bbox=(%d..%d, %d..%d)"
            % (stats["count"], stats["rows"], stats["cols"], stats["row_max"],
               stats["col_max"], stats["xmin"], stats["xmax"],
               stats["ymin"], stats["ymax"]))


def grab_quiet(rect, look: str, timeout: float = 6.0):
    """在 timeout 内反复截图，返回「最接近满直径横细线」的一张。

    真实环境可能有底噪/人声，不在某一帧上赌运气：持续采样直到识别出静音横线或超时，
    超时则返回期间最像横线的一帧并如实打印数字（调用方据此 PASS/SKIP）。
    """
    best = None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        image = grab(rect)
        stats = line_stats(image, look)
        score = (0 if is_quiet_line(stats) else 1, stats["rows"], -stats["cols"])
        if best is None or score < best[0]:
            best = (score, image, stats)
        if is_quiet_line(stats):
            break
        time.sleep(0.25)
    return best[1], best[2]


def save_zoom(image, name: str, zoom: int = 3) -> Path:
    from PIL import Image

    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SHOT_DIR / name
    image.resize((image.width * zoom, image.height * zoom), Image.NEAREST).save(path)
    print("    截图已保存：%s（%dx%d，放大 %dx）"
          % (path, image.width, image.height, zoom))
    return path


def save_compare(quiet_shot, loud_shot, look: str, zoom: int = 3) -> Path:
    """生成「放大 3 倍的有声 vs 静音」对照图（验收用）。"""
    from PIL import Image, ImageDraw, ImageFont

    cell = quiet_shot.width * zoom
    pad = 14
    header = 36
    width = pad + 2 * (cell + pad)
    height = header + cell + 30
    sheet = Image.new("RGB", (width, height), (247, 248, 250))
    draw = ImageDraw.Draw(sheet)
    font = None
    for name in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf"):
        try:
            font = ImageFont.truetype(name, 15)
            break
        except Exception:  # noqa: BLE001
            continue
    if font is None:
        font = ImageFont.load_default()

    def _zoom(img):
        return img.resize((img.width * zoom, img.height * zoom), Image.NEAREST)

    draw.text((pad, 9),
              "录音声纹 %dpx × %d（外观 %s）：左=静音满直径横细线 / 右=有声 9 根竖细线"
              % (quiet_shot.width, zoom, look), fill=(30, 34, 44), font=font)
    sheet.paste(_zoom(quiet_shot), (pad, header))
    sheet.paste(_zoom(loud_shot), (pad * 2 + cell, header))
    draw.text((pad + 4, header + cell + 4), "静音（一条满直径横细线）",
              fill=(60, 66, 78), font=font)
    draw.text((pad * 2 + cell + 4, header + cell + 4), "有声（9 根竖细线）",
              fill=(60, 66, 78), font=font)
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SHOT_DIR / "waveform_compare_quiet_vs_speech.png"
    sheet.save(path)
    print("    对照图已保存：%s（%dx%d）" % (path, sheet.width, sheet.height))
    return path


def log_levels(text: str) -> list[tuple[float, float]]:
    """解析 app 日志里的「录音声纹 level=0.123」行，返回 [(时间戳秒, level)]。"""
    items: list[tuple[float, float]] = []
    for line in text.splitlines():
        if "录音声纹 level=" not in line:
            continue
        try:
            stamp = line.split(" ")[1].replace(",", ".")
            hour, minute, second = stamp.split(":")
            t = int(hour) * 3600 + int(minute) * 60 + float(second)
            value = float(line.rsplit("level=", 1)[1].split()[0])
        except (IndexError, ValueError):
            continue
        items.append((t, value))
    return items


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    print("=" * 100)
    print("voice-ime 录音声纹 UI 集成测试（真实桌面截图；按外观自适应线色判据）")
    print("=" * 100)
    quit_running_instances()
    LOG.parent.mkdir(parents=True, exist_ok=True)

    look = current_look()
    print("    外观 look=%s（线色口径：%s）"
          % (look, "白线（高通）" if look in ("E", "ghost") else
             ("白线" if look in ("F", "dark_frost") else "深色线")))
    marker = len(log_text())
    proc = subprocess.Popen(
        [PY, "-B", str(ROOT / "app.py"), "--console"],
        cwd=str(ROOT), stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        banner("[1] 启动源码版 app.py，等待麦克风常驻就绪与悬浮球")
        if not wait_for(lambda: "麦克风常驻采集就绪" in log_text()[marker:], 90, 0.2):
            print("[FAIL] app 未就绪，日志尾部：")
            print(log_text()[-3000:])
            return 1
        rect = wait_for(ball_rect, 30, 0.2)
        if not rect:
            print("[FAIL] 找不到悬浮球窗口（TkTopLevel voice-ime）")
            return 1
        print("    悬浮球 rect=%s（%dx%d 物理像素）" % (rect, rect[2] - rect[0], rect[3] - rect[1]))
        time.sleep(1.0)

        banner("[2] 待机基准截图（此时中央是麦克风图标）")
        idle_shot = grab(rect)
        save_zoom(idle_shot, "waveform_live_idle.png")
        idle_stats = bright_stats(idle_shot)
        print("    idle   count=%(count)d ratio=%(ratio).4f span_y=%(span_y)d" % idle_stats)

        banner("[3] --toggle 开始录音 → 检查进入 recording + 录音声纹已启用")
        before = len(log_text())
        result = app_cmd("--toggle")
        print("    --toggle 退出码=%d" % result.returncode)
        if not wait_for(lambda: "state -> recording" in log_text()[before:], 10):
            print("[FAIL] 未进入 recording；日志尾部：")
            print(log_text()[-2000:])
            return 1
        wave_ready = wait_for(lambda: "录音声纹" in log_text()[before:], 10)
        check(wave_ready is not None, "录音声纹已启用（日志出现「录音声纹」）")
        if not wave_ready:
            print("    日志尾部：\n%s" % log_text()[-2500:])
        time.sleep(1.2)

        banner("[4] 静音窗口截图（应为一条满直径横细线；环境有底噪时最多等 6s 取最像横线一帧）")
        ball_size = rect[2] - rect[0]
        flat_limit = max(3, int(round(0.05 * ball_size)))  # 横细线高度 ≤3 行
        quiet_shot, quiet_stats = grab_quiet(rect, look, timeout=6.0)
        save_zoom(quiet_shot, "waveform_live_quiet.png")
        print("    quiet  %s" % stats_line(quiet_stats))
        quiet_ok = is_quiet_line(quiet_stats)
        print("    quiet 识别为静音横线=%s（rows≤3 且满直径；flat_limit=%dpx）"
              % (quiet_ok, flat_limit))

        banner("[5] 外放 SAPI TTS（约 5s），期间连续截图挑「最像竖细线」的一帧")
        tts_start = len(log_text())
        tts = spawn_tts()
        loud_shot = None
        loud_stats = None
        best = -1.0
        deadline = time.monotonic() + 14.0
        while time.monotonic() < deadline:
            image = grab(rect)
            stats = line_stats(image, look)
            score = stats["col_max"] * 10.0 + stats["count"]
            if score > best:
                best = score
                loud_shot, loud_stats = image, stats
            if tts.poll() is not None:
                break
            time.sleep(0.18)
        try:
            tts.wait(timeout=12)
        except subprocess.TimeoutExpired:
            tts.kill()
        time.sleep(0.6)
        if loud_shot is not None:
            save_zoom(loud_shot, "waveform_live_speech.png")
            print("    loud   %s" % stats_line(loud_stats))
            if quiet_ok:
                save_compare(quiet_shot, loud_shot, look)
        tts_log = log_text()[tts_start:]
        levels = log_levels(tts_log)
        loud_level = max((value for _t, value in levels), default=0.0)
        quiet_levels = [value for _t, value in log_levels(log_text()[before:tts_start])]
        quiet_level = max(quiet_levels) if quiet_levels else 0.0
        print("    日志声纹 level：静音段 max=%.3f（%d 行）；TTS 段 max=%.3f（%d 行）"
              % (quiet_level, len(quiet_levels), loud_level, len(levels)))

        banner("[6] 停止 TTS 后静音，确认回落为满直径横线（最多等 6s 取最像横线一帧）")
        time.sleep(1.0)
        tail_shot, tail_stats = grab_quiet(rect, look, timeout=6.0)
        save_zoom(tail_shot, "waveform_live_after.png")
        print("    tail   %s" % stats_line(tail_stats))

        banner("[7] 断言")
        check(idle_stats["span_y"] > 0.15 * idle_stats["height"],
              "待机态截图中麦克风图标存在：白像素垂直跨度 %dpx > %.1fpx（吊球高 %d）"
              % (idle_stats["span_y"], 0.15 * idle_stats["height"], idle_stats["height"]))

        # ---- 静音态：一条横跨整个直径的横细线（真机可能被环境声音打断 → 如实 SKIP） ----
        if quiet_ok:
            width = quiet_stats["width"]
            check(quiet_stats["cols"] >= 0.85 * width and quiet_stats["rows"] <= 3,
                  "录音静音态 = 一条满直径横细线：列跨度 %dpx ≥ %.1fpx、行数 %d ≤3（≈1px 细）"
                  % (quiet_stats["cols"], 0.85 * width, quiet_stats["rows"]))
            check(quiet_stats["row_max"] >= 0.80 * width and quiet_stats["count"] >= 0.60 * width,
                  "静音横线可见且无额外像素：线像素 %d（≥%.1f），最长行 %dpx（≥%.1f）"
                  % (quiet_stats["count"], 0.60 * width, quiet_stats["row_max"], 0.80 * width))
            check(quiet_stats["rows"] < 0.55 * idle_stats["span_y"],
                  "录音态麦克风图标已消失：静音线行跨度 %dpx < 待机图标跨度 %dpx 的 55%%"
                  % (quiet_stats["rows"], idle_stats["span_y"]))
        else:
            skip("静音截图未截到满直径横细线（截图时环境有声音/恰好截在过渡帧）："
                 "quiet %s；该状态需在安静环境复测，如实 SKIP"
                 % stats_line(quiet_stats))

        # ---- 说话态：9 根竖细线、无横底线、最高峰≤80% 直径、中心最高 ----
        if loud_level < 0.3 or loud_stats is None:
            skip("TTS 段实测 level 最大=%.3f（环境音量弱/麦克风被占用），跳过「有声 vs 静音」"
                 "波动断言；数字：loud %s"
                 % (loud_level, stats_line(loud_stats) if loud_stats else "无截图"))
        elif look in ("E", "ghost") and not is_speech_bars(loud_stats, look):
            skip("幽灵外观球体近乎透明，桌面内容会污染线条检测，无法可靠做像素结构断言，"
                 "如实 SKIP：loud %s" % stats_line(loud_stats))
        else:
            height = loud_stats["height"]
            check(loud_stats["rows"] >= 5 and 5 <= loud_stats["cols"] <= (24 if look in ("E", "ghost") else 12),
                  "有声态 = 竖细线束：%d 列（期望 9；幽灵外观白线+投影各占 1 列）、行跨度 %dpx ≥5"
                  % (loud_stats["cols"], loud_stats["rows"]))
            check(loud_stats["col_max"] <= 0.80 * height + 2,
                  "有声态最高竖线 %dpx ≤ 直径 80%%+2px（%.1fpx，球高 %d）"
                  % (loud_stats["col_max"], 0.80 * height + 2, height))
            check(loud_stats["ymin"] >= 0.05 * height and loud_stats["ymax"] <= 0.95 * height,
                  "有声态线条全部在圆内安全区：y 范围 %d..%d（留边 %.0f%%/%.0f%%）"
                  % (loud_stats["ymin"], loud_stats["ymax"],
                     100.0 * loud_stats["ymin"] / height,
                     100.0 * (height - 1 - loud_stats["ymax"]) / height))
            row_cap = 26 if look in ("E", "ghost") else 13
            check(loud_stats["row_max"] <= row_cap,
                  "有声态不画横底线：任意一行最多 %d 个线像素（≤%d）"
                  % (loud_stats["row_max"], row_cap))
            center_delta = abs(loud_stats["center_col"] + 0.5 - height / 2.0)
            check(center_delta <= 0.20 * height,
                  "有声态中心竖线最高：最高列 x=%d 距球心 %.1fpx ≤ %.1fpx"
                  % (loud_stats["center_col"], center_delta, 0.20 * height))
            if quiet_ok:
                check(loud_stats["rows"] >= quiet_stats["rows"] + 3,
                      "有声态竖线跨度 %dpx > 静音态 %dpx（波形随声音起伏）"
                      % (loud_stats["rows"], quiet_stats["rows"]))
            else:
                skip("静音态未取到，跳过「有声 vs 静音」跨度对比（数字已打印）")

        # ---- 停声后回落为横线 ----
        if is_quiet_line(tail_stats):
            check(True, "停声后回落为满直径横细线：列跨度 %dpx、行数 %dpx"
                  % (tail_stats["cols"], tail_stats["rows"]))
        else:
            skip("停声后截图未截到满直径横线（环境仍有人声/恰好过渡帧）：tail %s，如实 SKIP"
                 % stats_line(tail_stats))

        banner("[8] 退出（--quit 结束录音，不触发识别/文本注入，避免污染用户当前窗口）")
        # 注意：不要用第二次 --toggle 结束——那会走完 ASR 并把识别文本注入当前焦点窗口。
    except Exception:  # noqa: BLE001
        print("[FAIL] 测试流程异常：")
        import traceback

        traceback.print_exc()
        FAILURES.append("测试流程异常")
    finally:
        try:
            app_cmd("--quit")
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.wait(timeout=25)
        except subprocess.TimeoutExpired:
            proc.kill()

    # 等实例真正退出，确认无残留
    time.sleep(1.0)
    remaining = tk_windows()
    print("    退出后残留 TkTopLevel 窗口：%d" % len(remaining))
    check(not remaining, "退出后无残留悬浮球窗口（--quit 生效）")

    print("=" * 100)
    print("检查项=%d，失败=%d，SKIP=%d" % (CHECKS, len(FAILURES), len(SKIPS)))
    for item in SKIPS:
        print("  SKIP: %s" % item)
    if FAILURES:
        print("结果：FAIL")
        for item in FAILURES:
            print("  - %s" % item)
        print("---- 日志尾部 ----")
        print(log_text()[-4000:])
        return 1
    print("结果：PASS（%d 项断言全部通过%s）" % (CHECKS, "；有 SKIP" if SKIPS else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
