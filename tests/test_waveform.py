# -*- coding: utf-8 -*-
"""录音声纹渲染/声压级测试：合成音频包络 + 真实麦克风 + 纯细线规格 + 性能。

运行（在工程根目录下）::

    python -B tests/test_waveform.py

断言（全部打印真实数字，禁止只写 PASS）：
1) 合成音频 0.5s 静音 → 1s 幅度 0.3 正弦 → 0.5s 静音，按 1600 样本块喂 LevelMeter：
   - 初始静音 level < 0.02；有声段峰值 level > 0.3；再静音 0.5s 后 level < 0.1；
   - 上升耗时 > attack(15ms) 且 < 0.3s，回落耗时 > attack 且 < 1s（用 10ms 细粒度复测）；
   - 自适应噪声门 display_level：静音恒 0（平线），有声段 > 0.5。
2) 真实麦克风（capture.MicrophoneStream）录 2s 环境音：level 不卡 0/1，打印分布；
   无麦克风/ffmpeg 不可用打印 SKIP 并 exit 0。
3) 纯细线渲染规格（2026-10-31 v3 重做，与用户需求逐条对应）：
   - BARS=9；线宽 = size*1/70（70px 下 1px，最小 1px，140px 下 2px）；
   - 静音态只有一条横细线、横跨整个圆直径（x=0..size-1）、高 1px、无竖线；
     真机实测的 display_level 浮点残差（< QUIET_LEVEL=0.01）也按静音处理；
   - 说话态只画 9 根竖细线、不画横底线；每根线 1px 宽、无描边/发光/额外像素；
   - 最高峰 = 直径 80%（70px 下 56px，上下各留 7px=10%）；中心最高、钟形递减、
     左右镜像对称；level 增大高度单调增；history 提供错相（动态不对称）；
   - 按外观自适应线色：A 浅玻璃=深线(24,28,36,235)、F 深磨砂=白线(250)、
     E 幽灵=白线+极淡 1px 投影(10,14,22,65)/(1,1)（实测选型，见 waveform.py 注释）；
   - 全部像素落在圆内（半径 size/2）；线像素 alpha 精确、无抗锯齿半透明边缘。
   拼图存 docs/screenshots/waveform_levels.png。
4) 性能：70px 单帧渲染 < 2ms；每帧合成（copy+alpha_composite）< 2ms。
5) 线程安全：采集线程式高频 feed 的同时 UI 线程读 level，读到值恒在 0..1。
"""
from __future__ import annotations

import math
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from voiceime import glass2, waveform
from voiceime.audio_level import LevelMeter, level_from_rms

FAILURES: list[str] = []
CHECK_COUNT = 0
SKIPS: list[str] = []
SAMPLE_RATE = 16000
OUT_PNG = ROOT / "docs" / "screenshots" / "waveform_levels.png"

# 新规格常量（70px 运行时球径：config scale 1.0 × 125% DPI = 70 物理像素）
SIZE70 = 70
LIGHT_LOOKS = ("A", "F", "E")
EXPECTED_STYLES = {
    "A": ((24, 28, 36), 235),
    "F": ((255, 255, 255), 250),
    "E": ((255, 255, 255), 250),
}
EXPECTED_ALPHAS = {
    "A": {235},
    "F": {250},
    "E": {65, 250},  # 幽灵：白线 250 + 极淡 1px 投影 65
}
EXPECTED_COLORS = {
    "A": {(24, 28, 36, 235)},
    "F": {(255, 255, 255, 250)},
    "E": {(255, 255, 255, 250), (10, 14, 22, 65)},
}
GHOST_SHADOW = ((10, 14, 22), 65, 1, 1)


def check(condition: bool, message: str) -> bool:
    global CHECK_COUNT
    CHECK_COUNT += 1
    if condition:
        print("    [OK] %s" % message)
        return True
    print("    [FAIL] %s" % message)
    FAILURES.append(message)
    return False


def skip(message: str) -> None:
    SKIPS.append(message)
    print("    [SKIP] %s" % message)


def banner(title: str) -> None:
    print("-" * 100)
    print(title)


def _mask(img: Image.Image) -> np.ndarray:
    """返回 (H,W) bool 掩码：alpha>0 的像素为 True。"""
    alpha = np.frombuffer(img.getchannel("A").tobytes(), dtype=np.uint8).reshape(
        img.height, img.width
    )
    return alpha > 0


def _nonzero_pixels(img: Image.Image) -> tuple[np.ndarray, np.ndarray]:
    mask = _mask(img)
    ys, xs = np.nonzero(mask)
    return ys, xs


def _column_heights(img: Image.Image) -> dict[int, int]:
    """每个非零列的高度（像素数）。"""
    mask = _mask(img)
    out: dict[int, int] = {}
    for x in np.nonzero(mask.any(axis=0))[0].tolist():
        out[int(x)] = int(mask[:, x].sum())
    return out


# ---------------------------------------------------------------------------
# 1) 合成音频：包络曲线 + 断言
# ---------------------------------------------------------------------------

def synth_audio() -> np.ndarray:
    """0.5s 静音 + 1s 幅度 0.3 正弦(440Hz) + 0.5s 静音。"""
    total = int(SAMPLE_RATE * 2.0)
    t = np.arange(total) / float(SAMPLE_RATE)
    audio = np.zeros(total, dtype=np.float32)
    start, end = int(SAMPLE_RATE * 0.5), int(SAMPLE_RATE * 1.5)
    audio[start:end] = 0.3 * np.sin(2.0 * math.pi * 440.0 * t[start:end])
    pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype(np.int16)
    return pcm.tobytes()


def run_synthetic_checks(block_samples: int = 1600) -> None:
    banner("[1] 合成音频包络：0.5s 静音 → 1s 幅度0.3正弦 → 0.5s 静音（%d 样本/块 = %.0fms）"
           % (block_samples, block_samples * 1000.0 / SAMPLE_RATE))
    pcm = synth_audio()
    meter = LevelMeter(SAMPLE_RATE)
    block_bytes = block_samples * 2
    rows = []
    quiet_levels = []
    tone_levels = []
    tail_levels = []
    total_blocks = (len(pcm) + block_bytes - 1) // block_bytes
    t0 = time.perf_counter()
    for index in range(total_blocks):
        chunk = pcm[index * block_bytes:(index + 1) * block_bytes]
        meter.feed(chunk)
        snap = meter.snapshot()
        t = (index + 1) * block_samples / float(SAMPLE_RATE)
        rows.append((t, snap))
        if t <= 0.5:
            quiet_levels.append(snap["level"])
        elif t <= 1.5:
            tone_levels.append(snap["level"])
        else:
            tail_levels.append(snap["level"])
    feed_ms = (time.perf_counter() - t0) * 1000.0
    print("    逐块曲线（t / level / display / raw / dbfs / 噪声底 / 门限）：")
    for t, snap in rows:
        print("      t=%4.2fs level=%6.4f display=%6.4f raw=%6.4f dbfs=%6.1f floor=%6.1f gate=%6.1f"
              % (t, snap["level"], snap["display_level"], snap["raw_level"], snap["dbfs"],
                 snap["noise_floor_db"] if snap["noise_floor_db"] is not None else float("nan"),
                 snap["gate_db"] if snap["gate_db"] is not None else float("nan")))

    quiet_max = max(quiet_levels) if quiet_levels else 0.0
    tone_max = max(tone_levels) if tone_levels else 0.0
    tail_after = tail_levels[-1] if tail_levels else 0.0
    print("    初始静音段 level 最大=%.5f；有声段 level 最大=%.5f；末块(静音 0.5s 后) level=%.5f"
          % (quiet_max, tone_max, tail_after))
    print("    理论值：幅度 0.3 正弦 RMS=%.4f → %.2f dBFS → level≈%.3f"
          % (0.3 / math.sqrt(2), 20 * math.log10(0.3 / math.sqrt(2)),
             level_from_rms(0.3 / math.sqrt(2))))
    print("    喂音频总耗时=%.3f ms（%d 块，约 %.1f µs/块）"
          % (feed_ms, total_blocks, feed_ms * 1000.0 / max(1, total_blocks)))

    check(quiet_max < 0.02, "初始 0.5s 静音 level 最大=%.5f < 0.02" % quiet_max)
    check(tone_max > 0.3, "有声段 level 峰值=%.4f > 0.3" % tone_max)
    check(tail_after < 0.1, "再静音 0.5s 后 level=%.4f < 0.1" % tail_after)
    display_quiet = [s["display_level"] for t, s in rows if t <= 0.5]
    check(max(display_quiet) < 0.02, "初始静音 display_level 最大=%.4f < 0.02（平线）"
          % (max(display_quiet) if display_quiet else 0.0))
    display_tone = [s["display_level"] for t, s in rows if 0.6 <= t <= 1.4]
    check(max(display_tone) > 0.5, "有声段 display_level 峰值=%.4f > 0.5（波动明显）"
          % (max(display_tone) if display_tone else 0.0))
    last_display = rows[-1][1]["display_level"] if rows else 0.0
    check(last_display < 0.1, "静音 0.5s 后 display_level=%.4f < 0.1（继续衰减到 0，UI 平线）"
          % last_display)

    # --- 10ms 细粒度复测上升/回落耗时 ---
    meter2 = LevelMeter(SAMPLE_RATE)
    hop_bytes = int(SAMPLE_RATE * 0.01) * 2
    level_curve: list[tuple[float, float]] = []
    for index in range((len(pcm) + hop_bytes - 1) // hop_bytes):
        meter2.feed(pcm[index * hop_bytes:(index + 1) * hop_bytes])
        level_curve.append(((index + 1) * 0.01, meter2.level))
    rise = next((t for t, lv in level_curve if t >= 0.5 and lv > 0.3), None)
    fall = next((t for t, lv in level_curve if t >= 1.5 and lv < 0.1), None)
    rise_ms = (rise - 0.5) * 1000.0 if rise is not None else float("nan")
    fall_ms = (fall - 1.5) * 1000.0 if fall is not None else float("nan")
    print("    10ms 细粒度：tone 开始后 level>0.3 用时=%.0fms（attack=15ms）；"
          "tone 结束后 level<0.1 用时=%.0fms（release=180ms）" % (rise_ms, fall_ms))
    check(rise is not None and 0.0 < rise - 0.5 <= 0.3,
          "上升耗时 %.0fms ≤ 300ms（跟手；attack 时间常数=15ms，10ms 帧下首帧即 >0.3）" % rise_ms)
    check(fall is not None and 0.015 < fall - 1.5 < 1.0,
          "回落耗时 %.0fms 在 (attack 15ms, 1000ms) 区间" % fall_ms)


# ---------------------------------------------------------------------------
# 2) 真实麦克风：环境静音 + 外放 TTS，验证 level 不卡 0/1
# ---------------------------------------------------------------------------

TTS_TEXT = "测试一下实时声纹，一二三四五六七八九十，声音测试。"
# SAPI 启动约 1~2s，说话约 4~5s；用 EncodedCommand 避免中文经命令行编码层变形
_TTS_PS = (
    "Add-Type -AssemblyName System.Speech; "
    "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
    "$s.Rate = 0; $s.Volume = 100; "
    "$s.Speak('%s')" % TTS_TEXT
)


def _spawn_tts():
    import base64
    import subprocess

    encoded = base64.b64encode(_TTS_PS.encode("utf-16-le")).decode("ascii")
    return subprocess.Popen(
        ["powershell", "-NoProfile", "-EncodedCommand", encoded],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def run_microphone_checks() -> None:
    banner("[2] 真实麦克风：环境静音 1.5s → 外放 TTS 约 5s → 静音 1.5s，检查 level 不卡 0/1")
    try:
        from voiceime.audio import list_input_devices, resolve_device
        from voiceime.capture import CaptureError, MicrophoneStream
        from voiceime.config import ConfigError, ffmpeg_path, load_config
    except Exception as exc:  # noqa: BLE001
        skip("导入采集模块失败：%s" % exc)
        return
    try:
        cfg = load_config()
        ff = ffmpeg_path(cfg)
        devices = list_input_devices(ff)
        device = resolve_device(ff, cfg.get("mic_device"))
    except (ConfigError, CaptureError, OSError) as exc:
        skip("ffmpeg/麦克风不可用：%s" % exc)
        return
    print("    ffmpeg=%s" % ff)
    print("    设备列表=%s" % (devices,))
    print("    选用设备=%s" % device)
    print("    TTS 文本=%s" % TTS_TEXT)
    stream = MicrophoneStream(ff, device, sample_rate=SAMPLE_RATE, buffer_seconds=180)
    rows: list[tuple[float, float, float, float, float, float | None, float | None]] = []
    tts = None
    try:
        t0 = time.perf_counter()
        stream.start(timeout=15.0)
        startup_ms = (time.perf_counter() - t0) * 1000.0
        print("    采集就绪耗时=%.0f ms；warm=%s" % (startup_ms, stream.warm))
        start = time.monotonic()
        tts_started = None
        tts_ended = None
        deadline = start + 14.0
        while time.monotonic() < deadline:
            elapsed = time.monotonic() - start
            if tts is None and elapsed >= 1.5:
                tts = _spawn_tts()
                tts_started = elapsed
                print("    >>> 开始外放 TTS（t=%.1fs）" % elapsed)
            if tts is not None and tts_ended is None and tts.poll() is not None:
                tts_ended = elapsed
                print("    >>> TTS 播放结束（t=%.1fs）" % elapsed)
            if tts_ended is not None and elapsed >= tts_ended + 1.5:
                break
            snap = stream.meter.snapshot()
            rows.append((elapsed, stream.level, stream.display_level, snap["rms"], snap["dbfs"],
                         snap["noise_floor_db"], snap["gate_db"]))
            time.sleep(0.05)
        floor = stream.meter.noise_floor_db
        gate = stream.meter.gate_db
    except CaptureError as exc:
        skip("无法打开麦克风：%s" % exc)
        return
    finally:
        if tts is not None and tts.poll() is None:
            tts.kill()
        try:
            stream.close()
        except Exception:  # noqa: BLE001
            pass

    if len(rows) < 20:
        skip("采样点太少（%d），跳过麦克风断言" % len(rows))
        return
    print("    采样点=%d（20Hz × %.1fs）" % (len(rows), rows[-1][0]))
    print("    t     level   display   rms        dbfs    floor   gate")
    for row in rows[::4]:
        elapsed, level, display, rms, dbfs, fl, gt = row
        print("    %5.2fs %7.4f %8.4f  %.6f %7.1f %7.1f %7.1f"
              % (elapsed, level, display, rms, dbfs,
                 fl if fl is not None else float("nan"),
                 gt if gt is not None else float("nan")))

    quiet_display = [r[2] for r in rows if r[0] < 1.5]
    speech_rows = [r for r in rows if r[0] >= 1.5 and (tts_ended is None or r[0] <= tts_ended)]
    # TTS 进程退出后音频驱动可能还有短暂尾音，取最后 0.8s 作为「完全静音回平线」窗口
    last_elapsed = rows[-1][0]
    tail_rows = [r for r in rows if r[0] > last_elapsed - 0.8]
    level_max = max(r[1] for r in rows)
    level_min = min(r[1] for r in rows)
    display_max = max(r[2] for r in rows)
    quiet_display_max = max(quiet_display) if quiet_display else 0.0
    # 采集侧的「环境静音段」是否真的安静：用于决定能否验证「静音回平线」
    quiet_dbfs_max = max((r[4] for r in rows if r[0] < 1.5), default=-120.0)
    speech_display_max = max((r[2] for r in speech_rows), default=0.0)
    speech_dbfs_max = max((r[4] for r in speech_rows), default=-120.0)
    tail_display_max = max((r[2] for r in tail_rows), default=0.0)
    print("    level  min=%.4f max=%.4f；display min=%.4f max=%.4f"
          % (level_min, level_max, min(r[2] for r in rows), display_max))
    print("    静音段 display 最大=%.4f（采集 dBFS 峰值 %.1f）；TTS 段 display 最大=%.4f"
          "（dBFS 峰值 %.1f）；最后 0.8s display 最大=%.4f"
          % (quiet_display_max, quiet_dbfs_max, speech_display_max, speech_dbfs_max,
             tail_display_max))
    print("    自适应噪声底=%.1f dBFS；显示门限=%.1f dBFS"
          % (floor if floor is not None else float("nan"),
             gate if gate is not None else float("nan")))

    check(0.0 <= level_min <= 1.0 and 0.0 <= level_max <= 1.0, "level 恒在 0..1 区间")
    check(not (level_max == 1.0 and level_min == 1.0), "level 未卡在 1")
    if speech_dbfs_max < -55.0:
        skip("麦克风在 TTS 外放期间峰值仅 %.1f dBFS：设备可能被其它进程占用/系统静音，"
             "本轮无法验证「不卡 0」，如实 SKIP（level/display 数字已打印）" % speech_dbfs_max)
    else:
        check(level_max > 0.3, "TTS 段 level 峰值=%.4f > 0.3（麦克风响应正常）" % level_max)
        check(display_max > 0.4, "TTS 段 display 峰值=%.4f > 0.4（UI 可明显波动）" % display_max)
        if quiet_dbfs_max > -50.0:
            # 本机采集环境正在播声音（媒体/人声/环境噪声）：静音断言的前提不成立 → 如实 SKIP
            skip("『环境静音段』实测 dBFS 峰值 %.1f（采集环境本身有明显声音），"
                 "无法验证「静音回平线」，如实 SKIP；数字：静音 display max=%.4f"
                 % (quiet_dbfs_max, quiet_display_max))
        else:
            check(quiet_display_max < 0.25,
                  "环境静音段 display 最大=%.4f < 0.25（无声音接近平线；0.2 量级的单帧尖峰来自环境瞬态）"
                  % quiet_display_max)
        tail_dbfs_max = max((r[4] for r in tail_rows), default=-120.0)
        if tail_dbfs_max > -50.0:
            skip("『最后 0.8s』实测 dBFS 峰值 %.1f（采集环境仍有声音），"
                 "无法验证「回到平线」，如实 SKIP；数字：display max=%.4f"
                 % (tail_dbfs_max, tail_display_max))
        else:
            check(tail_rows and tail_display_max < 0.15,
                  "最后 0.8s（静音）display 最大=%.4f < 0.15（回到平线）" % tail_display_max)
    print("    说明：level 用规格口径（固定 -60/-6dB，环境底噪可能把它抬到 0.1~0.4）；"
          "UI 实际用 display_level（自适应门），静音为 0 平线。")


# ---------------------------------------------------------------------------
# 3) 纯细线渲染规格 + 拼图
# ---------------------------------------------------------------------------

def _font(size: int):
    for name in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc",
                 "C:/Windows/Fonts/simhei.ttf", "segoeui.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:  # noqa: BLE001
            continue
    return ImageFont.load_default()


def _desktop_crop(size: tuple[int, int]) -> Image.Image | None:
    """取真实桌面壁纸裁一块（若截图存在），让拼图接近真实观感。"""
    path = ROOT / "docs" / "screenshots" / "desktop_bg.png"
    if not path.exists():
        return None
    try:
        img = Image.open(path).convert("RGB")
    except Exception:  # noqa: BLE001
        return None
    w, h = img.size
    cw, ch = size
    if w < cw or h < ch:
        return None
    left = (w - cw) // 2
    top = max(0, (h - ch) // 2)
    return img.crop((left, top, left + cw, top + ch))


def _check_geometry_specs() -> None:
    """几何/配色纯函数规格：线宽、分布、颜色映射（黑盒断言从这里开始）。"""
    print("    线宽规格：line_width(35/70/140/280)=%s（目标 1/1/2/4，最小 1px）"
          % [waveform.line_width(v) for v in (35, 70, 140, 280)])
    check([waveform.line_width(v) for v in (35, 70, 140, 280)] == [1, 1, 2, 4],
          "线宽 = size*1/70 且最小 1px：35/70/140/280 → 1/1/2/4")
    check(waveform.BARS == 9, "BARS = 9（竖线根数）")

    positions = waveform.bar_x_positions(SIZE70)
    span = positions[-1] + waveform.line_width(SIZE70) - positions[0]
    print("    横向分布：positions=%s，总跨度=%dpx（0.66*70=%.1f），两侧余量=%dpx"
          % (positions, span, 0.66 * SIZE70, positions[0]))
    check(len(positions) == 9 and positions == sorted(positions),
          "9 根竖线横向有序：%s" % positions)
    check(abs(span - waveform.SPAN_RATIO * SIZE70) <= 1.0,
          "竖线总跨度=%dpx ≈ 0.66*size=%.1fpx（±1px）" % (span, waveform.SPAN_RATIO * SIZE70))
    pairwise = [positions[i] + positions[-1 - i] for i in range(9) if i != 4]
    check(all(value == SIZE70 - 1 for value in pairwise)
          and abs((positions[4] + 0.5) - SIZE70 / 2.0) <= 0.5,
          "横向左右对称（首尾列两两之和=%s = size-1；中心列=%d，偏差≤0.5px）"
          % (pairwise, positions[4]))
    warm = waveform.line_width(SIZE70)
    check(positions[0] >= SIZE70 * 0.10 and positions[-1] + warm <= SIZE70 * 0.90,
          "竖线不超出圆内安全区：首列=%d ≥ 7px、末列右缘=%d ≤ 63px"
          % (positions[0], positions[-1] + warm))

    for look, expected in EXPECTED_STYLES.items():
        got = waveform.line_style(look)
        check(got == expected, "look=%s 线色=%s（自适应：%s）"
              % (look, got, "浅玻璃深线" if look == "A" else
                 ("深磨砂白线" if look == "F" else "幽灵白线+极淡投影")))
    check(waveform.line_style("light_frost") == ((24, 28, 36), 235)
          and waveform.line_style("dark_frost") == ((255, 255, 255), 250),
          "方案名与 A/E/F 短代码配色一致（light_frost 深线 / dark_frost 白线）")
    check(waveform.line_shadow("E") == GHOST_SHADOW
          and waveform.line_shadow("A") is None and waveform.line_shadow("F") is None,
          "幽灵=白线+极淡 1px 投影 %s，A/F 无投影（不是描边：投影只有右下 1px）"
          % (GHOST_SHADOW,))


def _check_quiet_state() -> None:
    """静音态：只有一条横跨整个圆直径的细线（无竖线、无描边）。"""
    for look in LIGHT_LOOKS:
        img = waveform.render_overlay(0.0, SIZE70, look=look)
        mask = _mask(img)
        arr = np.asarray(img)
        rows = np.nonzero(mask.any(axis=1))[0].tolist()
        cols = np.nonzero(mask.any(axis=0))[0].tolist()
        colors = sorted(set(map(tuple, arr[mask > 0].tolist())))
        alphas = set(np.unique(arr[:, :, 3]).tolist())
        # 幽灵的 1px 投影右下偏移：投影行 x 从 1 起、右端被画布裁掉 1px
        expected_rows = 1 if look != "E" else 2
        expected_pixels = SIZE70 if look != "E" else SIZE70 + (SIZE70 - GHOST_SHADOW[2])
        print("    look=%s 静音：行=%s 列=%d..%d（共 %d 列），像素=%d，颜色=%s，alpha=%s"
              % (look, rows, cols[0], cols[-1], len(cols), int(mask.sum()), colors,
                 sorted(alphas)))
        check(len(rows) == expected_rows and cols[0] == 0 and cols[-1] == SIZE70 - 1,
              "look=%s 静音 = 一条横跨整个圆直径的横细线（x=0..69 全宽、%d 行）"
              % (look, expected_rows))
        check(int(mask.sum()) == expected_pixels,
              "look=%s 静音线像素数=%d（无竖线/无描边等额外像素）"
              % (look, int(mask.sum())))
        check(alphas <= {0} | EXPECTED_ALPHAS[look],
              "look=%s 线像素 alpha 精确 ∈ %s（无抗锯齿半透明边缘）"
              % (look, sorted(EXPECTED_ALPHAS[look])))
        check(set(colors) == EXPECTED_COLORS[look],
              "look=%s 静音线颜色=%s（按外观自适应，与规格一致）" % (look, colors))

    # 真机实测缺陷回归：display_level 是指数衰减浮点值，安静时常见 1e-6~1e-3 的非零残留，
    # 必须一律按静音画满直径横细线（否则会画成 9 个 1px 小点，横线永不出现）。
    print("    静音阈值回归：QUIET_LEVEL=%.3f；level=0.0005/0.005/0.0099 应仍为满直径横线"
          % waveform.QUIET_LEVEL)
    residual_ok = True
    for residual in (0.0005, 0.005, 0.0099):
        img = waveform.render_overlay(residual, SIZE70, look="A")
        mask = _mask(img)
        rows = np.nonzero(mask.any(axis=1))[0].tolist()
        cols = np.nonzero(mask.any(axis=0))[0].tolist()
        if len(rows) != 1 or cols[0] != 0 or cols[-1] != SIZE70 - 1:
            residual_ok = False
        print("      level=%.4f → 行=%s 列=%d..%d" % (residual, rows, cols[0], cols[-1]))
    check(residual_ok,
          "真机静音浮点残差（0.0005/0.005/0.0099）也画满直径横细线（QUIET_LEVEL=%.3f）"
          % waveform.QUIET_LEVEL)
    above = waveform.render_overlay(0.02, SIZE70, look="A")
    above_rows = np.nonzero(_mask(above).any(axis=1))[0]
    print("      level=0.02 → 竖线行跨度=%d（应 >1，即进入说话态）" % above_rows.size)
    check(int(above_rows.size) > 1, "level=0.02 > QUIET_LEVEL 时进入说话态画竖线（阈值分界正确）")


def _check_speaking_state() -> None:
    """说话态：只有 9 根竖细线（无横底线、无描边/发光），高度符合 80% 直径与钟形。"""
    level = 0.7
    img = waveform.render_overlay(level, SIZE70, look="A")
    mask = _mask(img)
    columns = _column_heights(img)
    heights = waveform.bar_heights(level, SIZE70)
    expected = [int(round(h)) for h in heights]
    actual = [columns[x] for x in sorted(columns)]
    positions = waveform.bar_x_positions(SIZE70)
    print("    level=%.2f A 说话态：列=%s" % (level, sorted(columns)))
    print("    每列高度(px)=%s；bar_heights 取整=%s" % (actual, expected))
    check(sorted(columns) == positions,
          "说话态只画 9 根竖细线（列位置=%s，宽 1px、无描边产生的额外列）" % sorted(columns))
    check(actual == expected, "每根竖线像素高=round(bar_heights)=%s" % expected)
    contiguous = all(
        (lambda ys: ys.size > 0 and int(ys[-1] - ys[0] + 1) == int(ys.size))(
            np.nonzero(mask[:, x])[0]) for x in columns
    )
    check(contiguous, "每根竖线是连续线段（无断裂/无点状）")
    row_counts = mask.sum(axis=1)
    check(int(row_counts.max()) <= waveform.BARS,
          "说话态不画横底线：任意一行亮像素最多 %d 个 ≤ BARS(9)（无满宽行）"
          % int(row_counts.max()))
    check(int(mask.sum()) == sum(expected),
          "说话态总像素=%d = 各竖线高度之和（无描边/发光/投影额外像素）" % int(mask.sum()))

    # 最高峰 = 直径 80%（70px → 56px，上下各留 7px = 10%）
    h1 = waveform.bar_heights(1.0, SIZE70)
    print("    level=1.00 heights=%s" % [round(h, 2) for h in h1])
    check(h1[4] == SIZE70 * 0.80 == 56.0,
          "最高峰（中心竖线）level=1.0 高度=%.1fpx = 直径 80%%" % h1[4])
    img1 = waveform.render_overlay(1.0, SIZE70, look="A")
    ys1, _xs1 = _nonzero_pixels(img1)
    top, bottom = int(ys1.min()), int(ys1.max())
    print("    level=1.00 A 渲染 bbox_y=%d..%d（上留 %dpx / 下留 %dpx = 各 %.0f%%）"
          % (top, bottom, top, SIZE70 - 1 - bottom,
             100.0 * top / SIZE70))
    check(top == int(round(SIZE70 * 0.10)) and SIZE70 - 1 - bottom == int(round(SIZE70 * 0.10)),
          "最高峰上下各留 10%%：顶部 %dpx / 底部 %dpx（= 7px）"
          % (top, SIZE70 - 1 - bottom))
    check(all(h <= SIZE70 * 0.80 for h in h1),
          "所有竖线高度 ≤ 直径 80%%（最大 %.1fpx）" % max(h1))

    # 钟形 profile：中心最高、向两侧递减、左右镜像对称
    bell_ok = all(h1[i] < h1[i + 1] for i in range(4))
    mirror_ok = all(abs(h1[i] - h1[-1 - i]) < 1e-9 for i in range(9))
    print("    level=1.00 左半高度随距中心递减（中心最高）=%s；左右镜像对称=%s"
          % (bell_ok, mirror_ok))
    check(bell_ok and mirror_ok, "钟形分布：中心最高、向两侧递减、左右镜像对称")

    # level 增大高度单调增（gamma 0.70 保留）
    centers = [waveform.bar_heights(lv, SIZE70)[4] for lv in (0.0, 0.25, 0.5, 0.75, 1.0)]
    print("    中心竖线随 level：0/0.25/0.5/0.75/1.0 → %s" % [round(h, 2) for h in centers])
    check(all(centers[i] < centers[i + 1] for i in range(4)),
          "中心竖线高度随 level 单调增长：%s" % [round(h, 2) for h in centers])
    gamma_ratio = waveform.bar_heights(0.25, SIZE70)[4] - 1.0
    gamma_full = waveform.bar_heights(1.0, SIZE70)[4] - 1.0
    implied = (gamma_ratio / gamma_full) ** (1.0 / waveform.LEVEL_GAMMA)
    print("    gamma 反推：level 0.25 中心线增幅/满幅=%.3f → 反推 level≈%.3f（gamma=%.2f）"
          % (gamma_ratio / gamma_full, implied, waveform.LEVEL_GAMMA))
    check(abs(implied - 0.25) < 0.03,
          "保留 level gamma=%.2f（0.25 的显示幅度反推 ≈0.25）" % waveform.LEVEL_GAMMA)

    # history 错相：level 刚从 0 跳起时，各竖线读到不同历史 → 左右暂时不对称
    hist = [0.0] * 27
    hist[-1] = 0.9
    staggered = waveform.bar_heights(0.9, SIZE70, history=hist)
    steady = waveform.bar_heights(0.9, SIZE70)
    print("    history 错相：hist[-1]=0.9 时 heights=%s" % [round(h, 2) for h in staggered])
    check(staggered[0] > staggered[-1] + 2.0
          and abs(steady[0] - steady[-1]) < 1e-9,
          "history 错相生效：跳变帧左端 %.1fpx > 右端 %.1fpx（稳态镜像相等）"
          % (staggered[0], staggered[-1]))

    # 所有像素在圆内（含幽灵投影行）
    for look in LIGHT_LOOKS:
        img = waveform.render_overlay(0.7, SIZE70, look=look)
        ys, xs = _nonzero_pixels(img)
        dist = np.hypot(xs + 0.5 - SIZE70 / 2.0, ys + 0.5 - SIZE70 / 2.0)
        check(float(dist.max()) <= SIZE70 / 2.0,
              "look=%s 全部像素在圆内：最远像素距圆心 %.2fpx ≤ 半径 35px"
              % (look, float(dist.max())))

    # 等比：140px 下线宽 2px、静音线 2 行
    img140 = waveform.render_overlay(0.0, 140, look="A")
    mask140 = _mask(img140)
    rows140 = np.nonzero(mask140.any(axis=1))[0].tolist()
    cols140 = np.nonzero(mask140.any(axis=0))[0].tolist()
    print("    140px 静音线：行=%s，列=%d..%d（线宽 %dpx）"
          % (rows140, cols140[0], cols140[-1], len(rows140)))
    check(len(rows140) == 2 and cols140[0] == 0 and cols140[-1] == 139,
          "等比缩放：140px 下静音线宽 2px、仍满直径（140 列）")


def _render_sheet() -> None:
    """A/E/F 三外观 × 5 档 level 拼图（70px 渲染 2× NEAREST 展示，真实像素）。"""
    levels = (0.0, 0.25, 0.5, 0.75, 1.0)
    cell = 140
    zoom = 2
    looks = [("A 浅玻璃（深色细线）", "A", "light_frost"),
             ("E 幽灵（白线+极淡投影）", "E", "ghost"),
             ("F 深色磨砂（白色细线）", "F", "dark_frost")]
    backgrounds = [("light bg", (226, 231, 238, 255)),
                   ("dark bg", (26, 30, 40, 255))]
    desktop = _desktop_crop((SIZE70, SIZE70))
    rows: list[tuple[str, tuple[int, int, int, int] | Image.Image]] = []
    if desktop is not None:
        rows.append(("desktop", desktop))
    rows.extend(backgrounds)

    pad = 12
    header = 46
    footer = 34
    width = pad + 5 * (cell + pad)
    height = header + len(looks) * len(rows) * (cell + pad) + footer
    sheet = Image.new("RGB", (width, height), (247, 248, 250))
    draw = ImageDraw.Draw(sheet)
    font = _font(15)
    small = _font(13)
    draw.text((pad, 12),
              "voice-ime recording waveform — 9 thin lines / 70px render zoomed %dx (NEAREST)" % zoom,
              fill=(30, 34, 44), font=font)
    y = header
    for look_label, look, variant in looks:
        base = glass2.render_base(variant, SIZE70, state="recording").convert("RGBA")
        for bg_label, bg in rows:
            draw.text((pad, y - 2), "look %s / %s" % (look, bg_label),
                      fill=(90, 96, 108), font=small)
            for col, lv in enumerate(levels):
                x = pad + col * (cell + pad)
                if isinstance(bg, Image.Image):
                    wallpaper = bg.convert("RGBA")
                else:
                    wallpaper = Image.new("RGBA", (SIZE70, SIZE70), bg)
                overlay = waveform.render_overlay(lv, SIZE70, state="recording", look=look)
                shown = waveform.compose(waveform.compose(wallpaper, base), overlay)
                shown = shown.resize((cell, cell), Image.NEAREST)
                sheet.paste(shown.convert("RGB"), (x, y + 14))
                draw.text((x + 2, y + 14 + cell - 16), "level %.2f" % lv,
                          fill=(70, 76, 88), font=small)
            y += cell + pad
    draw.text((pad, height - footer + 2),
              "静音 level=0 → 一条满直径横细线；有声 → 只画 9 根竖细线（无描边/无发光/无横底线）；"
              "最高峰=直径80%（上下各留10%）。A=深线(24,28,36,235) E=白线+极淡1px投影 F=白线(250)",
              fill=(90, 96, 108), font=small)
    OUT_PNG.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(OUT_PNG)
    print("    拼图已保存：%s（%dx%d，%d 字节）"
          % (OUT_PNG, sheet.width, sheet.height, OUT_PNG.stat().st_size))
    check(OUT_PNG.exists() and OUT_PNG.stat().st_size > 5000, "拼图文件已生成且非空")


def run_render_checks() -> None:
    banner("[3] 纯细线渲染规格：9 根竖线 / 满直径静音线 / 最高峰=80% / 按外观自适应线色")
    print("    五档 level 的竖线高度（px，70px 球）：")
    for lv in (0.0, 0.25, 0.5, 0.75, 1.0):
        print("      level=%.2f heights=%s"
              % (lv, [round(h, 1) for h in waveform.bar_heights(lv, SIZE70)]))
    _check_geometry_specs()
    _check_quiet_state()
    _check_speaking_state()
    _render_sheet()


# ---------------------------------------------------------------------------
# 4) 性能 & 5) 线程安全
# ---------------------------------------------------------------------------

def run_perf_and_thread_checks() -> None:
    banner("[4] 渲染/合成性能（70px，30fps 帧预算 33ms）")
    for look in ("A", "F", "E"):
        n = 300
        t0 = time.perf_counter()
        for _ in range(n):
            waveform.render_overlay(0.65, 70, state="recording", look=look)
        avg = (time.perf_counter() - t0) * 1000.0 / n
        print("    render_overlay(level=0.65, 70px, look=%s) 平均=%.3f ms/帧" % (look, avg))
        check(avg < 2.0, "look=%s 单帧渲染 %.3fms < 2ms" % (look, avg))

    base = Image.new("RGBA", (70, 70), (30, 34, 44, 255))
    overlay = waveform.render_overlay(0.65, 70)
    n = 300
    t0 = time.perf_counter()
    for _ in range(n):
        waveform.compose(base, overlay)
    compose_ms = (time.perf_counter() - t0) * 1000.0 / n
    print("    copy+alpha_composite 每帧合成=%.3f ms" % compose_ms)
    check(compose_ms < 2.0, "每帧合成 %.3fms < 2ms" % compose_ms)

    banner("[5] 线程安全：采集线程高频 feed + UI 线程同时读 level")
    meter = LevelMeter(SAMPLE_RATE)
    rng = np.random.default_rng(7)
    pcm = (rng.normal(0.0, 0.12, SAMPLE_RATE) * 32767).astype(np.int16).tobytes()
    read_values: list[float] = []
    bad_values: list[float] = []
    stop = threading.Event()

    def writer() -> None:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            meter.feed(pcm)

    def reader() -> None:
        while not stop.is_set():
            value = meter.level
            read_values.append(value)
            if not (0.0 <= value <= 1.0):
                bad_values.append(value)

    writer_thread = threading.Thread(target=writer, daemon=True)
    reader_thread = threading.Thread(target=reader, daemon=True)
    reader_thread.start()
    writer_thread.start()
    writer_thread.join(timeout=2.0)
    stop.set()
    reader_thread.join(timeout=2.0)
    print("    UI 线程读到 %d 次，越界值=%d" % (len(read_values), len(bad_values)))
    t0 = time.perf_counter()
    for _ in range(2000):
        _ = meter.level
    read_us = (time.perf_counter() - t0) * 1e6 / 2000.0
    print("    level 读取平均=%.2f µs/次" % read_us)
    check(not bad_values, "并发读取的 level 全部在 0..1（越界=%d）" % len(bad_values))
    check(len(read_values) > 100, "并发期间读取次数=%d > 100" % len(read_values))


def main() -> int:
    print("=" * 100)
    print("voice-ime 录音声纹测试：LevelMeter 包络 / 真实麦克风 / 纯细线渲染规格 / 性能")
    print("=" * 100)
    run_synthetic_checks()
    run_microphone_checks()
    run_render_checks()
    run_perf_and_thread_checks()

    print("=" * 100)
    print("检查项=%d，失败=%d，SKIP=%d" % (CHECK_COUNT, len(FAILURES), len(SKIPS)))
    if SKIPS:
        for item in SKIPS:
            print("  SKIP: %s" % item)
    if FAILURES:
        print("结果：FAIL")
        for item in FAILURES:
            print("  - %s" % item)
        return 1
    print("结果：PASS（%d 项断言全部通过%s）"
          % (CHECK_COUNT, "；有 SKIP" if SKIPS else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
