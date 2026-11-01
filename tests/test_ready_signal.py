# -*- coding: utf-8 -*-
"""冷启动过场/就绪信号单元测试（不依赖麦克风/GUI 窗口）。

覆盖（2026-10-31 task-4）：
1) ready 提示音 WAV：内存生成的波形参数正确（44100Hz / 16bit / 单声道 / ~75ms /
   880Hz / 很轻的幅度 / 淡入淡出），且带缓存不重复生成；
2) 冷启动阈值：READY_MIN_MS=300ms，只有 >300ms 的过渡才给 ready 信号；
3) 过场文案长度（_short_message 限 12 字符）不被截断成怪句子；
4) 麦克风设备名缓存：第一次解析后复用（省 ffmpeg -list_devices 129~204ms），
   force_refresh 才重新枚举；缓存设备失效时自动清缓存重试一次；
5) 状态事件：rec_failed → error「麦克风打开失败」；rec_started 冷启动 → UI 收到
   「可以说话了」气泡（800ms）+ 提示音；暖启动 → 普通「请说话…」且不响；
6) UI 队列：set_state 支持 bubble_ms（冷启动提示/就绪提示的停留时长）；
7) capture 首批音频等待从 200ms 降到 120ms（常量 + 至少 100ms 不丢音）。

运行：python -B tests/test_ready_signal.py
"""
from __future__ import annotations

import math
import sys
import time
import wave
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import app as appmod  # noqa: E402
from voiceime import capture as capture_mod  # noqa: E402
from voiceime import config as cfgmod  # noqa: E402
from voiceime import ui as ui_mod  # noqa: E402

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


def banner(title: str) -> None:
    print("-" * 100)
    print(title)


class FakeUI:
    """记录 set_state 调用的假 UI。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def set_state(self, state, message="", bubble_ms=None) -> None:
        self.calls.append((state, message, bubble_ms))

    def set_level(self, level) -> None:  # pragma: no cover - 本测试不进入推流
        pass

    def set_level_available(self, available) -> None:
        pass

    def quit(self) -> None:  # pragma: no cover
        pass


def run_chime_checks() -> None:
    banner("[1] ready 提示音 WAV 参数（内存生成，很轻）")
    data = appmod._ready_chime_wav()
    check(isinstance(data, (bytes, bytearray)) and len(data) > 1000,
          "提示音是有效 WAV 内存数据（%d 字节）" % len(data))
    check(appmod._ready_chime_wav() is data, "提示音 WAV 带缓存（不重复生成）")
    with wave.open(BytesIO(data), "rb") as reader:
        channels = reader.getnchannels()
        sampwidth = reader.getsampwidth()
        framerate = reader.getframerate()
        frames = reader.getnframes()
        raw = reader.readframes(frames)
    duration_ms = frames * 1000.0 / framerate
    samples = [int.from_bytes(raw[i:i + 2], "little", signed=True)
               for i in range(0, len(raw), 2)]
    peak = max(abs(v) for v in samples)
    first = abs(samples[0])
    last = abs(samples[-1])
    zero_cross = sum(
        1 for a, b in zip(samples, samples[1:]) if (a >= 0) != (b >= 0)
    )
    est_freq = zero_cross / 2.0 / (duration_ms / 1000.0)
    rms = math.sqrt(sum(v * v for v in samples) / max(1, len(samples)))
    print("    channels=%d sampwidth=%d rate=%d frames=%d duration=%.1fms peak=%d(%.1f%%FS) "
          "first=%d last=%d zero_cross=%d 估频≈%.0fHz RMS=%.0f(%.1f%%FS)"
          % (channels, sampwidth, framerate, frames, duration_ms, peak,
             100.0 * peak / 32767.0, first, last, zero_cross, est_freq,
             rms, 100.0 * rms / 32767.0))
    check(channels == 1 and sampwidth == 2 and framerate == 44100,
          "格式正确：单声道/16bit/44100Hz")
    check(60.0 <= duration_ms <= 90.0, "时长 %.1fms 在 60~90ms（建议 60~80ms 量级内）" % duration_ms)
    check(abs(est_freq - appmod.CHIME_FREQ_HZ) <= 40.0,
          "频率估计 %.0fHz ≈ %.0fHz（880Hz 提示音）" % (est_freq, appmod.CHIME_FREQ_HZ))
    check(0.03 * 32767 <= peak <= 0.15 * 32767,
          "幅度峰值 %.1f%%FS 在 3%%~15%%（很轻，避免系统提示音那种大声）"
          % (100.0 * peak / 32767.0))
    fade = max(1, int(44100 * 0.012))
    check(first <= 0.25 * peak and last <= 0.25 * peak,
          "首尾淡入淡出生效：first=%d / last=%d ≤ 峰值 25%%（%d）"
          % (first, last, int(0.25 * peak)))
    edge_peak = max(abs(v) for v in samples[:fade])
    check(edge_peak <= peak, "淡入段幅度不超过峰值（无爆音起音）")
    check(100.0 * rms / 32767.0 <= 8.0,
          "整体 RMS %.1f%%FS ≤ 8%%（轻声提示，不扰民）" % (100.0 * rms / 32767.0))

    # 回归：CPython winsound 不支持 SND_MEMORY|SND_ASYNC（会抛 RuntimeError，
    # 2026-10-31 真机实测提示音因此没响）；修复后应在后台线程里用 SND_MEMORY 同步播放。
    import types

    fake = types.ModuleType("winsound")
    fake.SND_MEMORY = 0x00040000
    fake.SND_ASYNC = 0x00000001
    fake.SND_FILENAME = 0x00020000
    calls: list[tuple] = []

    def fake_play(data, flags):
        calls.append((len(data), flags))

    fake.PlaySound = fake_play
    old_winsound = sys.modules.get("winsound")
    sys.modules["winsound"] = fake
    try:
        app = appmod.VoiceIME(cfgmod.default_config(), with_ui=False)
        started = app._play_ready_chime()
        deadline = time.monotonic() + 2.0
        while not calls and time.monotonic() < deadline:
            time.sleep(0.01)
        flags = calls[0][1] if calls else 0
        print("    _play_ready_chime -> %s，winsound.PlaySound 调用=%s，flags=0x%X"
              % (started, calls, flags))
        check(started is True and bool(calls),
              "ready 提示音实际调用 winsound.PlaySound（真机不再静默）")
        check(bool(flags & fake.SND_MEMORY) and not bool(flags & fake.SND_ASYNC),
              "用 SND_MEMORY 同步播放（后台线程），规避 CPython「Cannot play asynchronously from memory」")
        cfg_off = cfgmod.default_config()
        cfg_off["ui"]["ready_chime"] = False
        calls.clear()
        app_off = appmod.VoiceIME(cfg_off, with_ui=False)
        check(app_off._play_ready_chime() is False and not calls,
              "config ui.ready_chime=False 时不播放提示音")
    finally:
        if old_winsound is not None:
            sys.modules["winsound"] = old_winsound
        else:
            sys.modules.pop("winsound", None)


def run_threshold_checks() -> None:
    banner("[2] 冷启动阈值 / 过场文案 / 配置默认值")
    print("    READY_MIN_MS=%.0f READY_BUBBLE_MS=%d LOADING_BUBBLE_MS=%d"
          % (appmod.READY_MIN_MS, appmod.READY_BUBBLE_MS, appmod.LOADING_BUBBLE_MS))
    cases = [(0.0, False), (100.0, False), (299.9, False), (300.1, True), (900.0, True)]
    results = [(ms, expected, appmod._should_signal_ready(ms)) for ms, expected in cases]
    print("    阈值判定：", [(round(ms), got) for ms, _e, got in results])
    check(all(got == expected for _ms, expected, got in results),
          "只有冷启动过渡 >300ms 才给 ready 信号（暖启动/快速路径不响不闪）")
    check(appmod.READY_MIN_MS == 300.0, "READY_MIN_MS = 300ms")
    check(600 <= appmod.READY_BUBBLE_MS <= 1000,
          "「可以说话了」气泡停留 %dms 在 600~1000ms" % appmod.READY_BUBBLE_MS)
    check(appmod.LOADING_BUBBLE_MS >= 3000,
          "冷启动提示气泡兜底 %dms ≥ 3000ms（不会在就绪前提前淡出）" % appmod.LOADING_BUBBLE_MS)

    texts = ["正在打开麦克风，请稍候…", "可以说话了", "麦克风打开失败", "正在加载模型，请稍候…"]
    print("    气泡文案（_short_message 限 12 字符）：")
    ok = True
    for text in texts:
        shown = ui_mod._short_message(text)
        print("      %r -> %r (%d 字)" % (text, shown, len(shown)))
        if shown != text:
            ok = False
    check(ok, "过场/就绪/失败文案都不被截断")
    check(cfgmod.default_config()["ui"].get("ready_chime") is True,
          "config 默认 ui.ready_chime = True（可关）")


def run_device_cache_checks() -> None:
    banner("[3] 麦克风设备名缓存（省 ffmpeg -list_devices 129~204ms）")
    from voiceime import audio as audio_mod

    app = appmod.VoiceIME(cfgmod.default_config(), with_ui=False)
    calls = {"n": 0}

    def fake_resolve(ffmpeg_path, configured):
        calls["n"] += 1
        return "Fake Mic %d" % calls["n"]

    original = audio_mod.resolve_device
    audio_mod.resolve_device = fake_resolve
    try:
        first = app._resolve_mic_device()
        second = app._resolve_mic_device()
        third = app._resolve_mic_device(force_refresh=True)
        print("    resolve 调用次数=%d；device 序列=%r %r %r"
              % (calls["n"], first, second, third))
        check(calls["n"] == 2, "前两次解析复用缓存（只调 1 次 resolve_devices），force 才重新枚举")
        check(first == second == "Fake Mic 1" and third == "Fake Mic 2",
              "缓存命中返回同一设备；force_refresh 重新枚举并更新缓存")
        check(app._mic_device_cached == "Fake Mic 2", "新解析结果已写回缓存")

        # 缓存失效重试：第一次 start 抛错（走缓存），清缓存重枚举后第二次成功
        class FakeStream:
            instances: list = []

            def __init__(self, ffmpeg_path, device, buffer_seconds=180):
                self.device = device
                self.started = 0
                self.warm = True
                FakeStream.instances.append(self)

            def start(self, timeout=10.0):
                self.started += 1
                if self is FakeStream.instances[0]:
                    raise RuntimeError("旧设备不存在（模拟 USB 拔插）")

        original_stream = capture_mod.MicrophoneStream
        capture_mod.MicrophoneStream = FakeStream
        try:
            app._mic_device_cached = "Stale Mic"
            stream = app._ensure_warm_capture()
            print("    重试后：device=%r，实例数=%d，capture 缓存=%r"
                  % (stream.device, len(FakeStream.instances), app._mic_device_cached))
            check(len(FakeStream.instances) == 2 and stream is FakeStream.instances[1],
                  "缓存设备 start 失败后自动重新枚举、只重试一次并成功")
            check(stream.device == "Fake Mic %d" % (calls["n"]),
                  "重试使用重新枚举到的新设备名（%s）" % stream.device)
            check(app._mic_device_cached == stream.device,
                  "重试成功后新设备名写回缓存")
        finally:
            capture_mod.MicrophoneStream = original_stream
            app._capture = None
    finally:
        audio_mod.resolve_device = original


def run_state_flow_checks() -> None:
    banner("[4] 状态事件：失败气泡 / 冷启动 ready 双信号 / 暖启动不响")
    app = appmod.VoiceIME(cfgmod.default_config(), with_ui=True)
    fake = FakeUI()
    app.set_ui(fake)
    app._toast_later = lambda *args, **kwargs: None  # 避免后台 toast 定时器干扰本测试

    # 4a) 打开麦克风失败 → error「麦克风打开失败」（不静默）
    app._handle_event(("rec_failed", "打开失败：设备被占用"))
    print("    失败路径 UI 调用：", fake.calls)
    check(fake.calls and fake.calls[-1][0] == "error" and fake.calls[-1][1] == "麦克风打开失败",
          "rec_failed → error 态 + 气泡「麦克风打开失败」")

    # 4b) 冷启动 >300ms 就绪 → 「可以说话了」(800ms) + 提示音
    called = {"chime": 0}
    app._play_ready_chime = lambda: called.__setitem__("chime", called["chime"] + 1) or True
    fake.calls.clear()
    app.state = "preparing"
    app._cold_start_at = time.monotonic() - 0.55  # 模拟已冷启动 550ms
    app._handle_event(("rec_started", 12345))
    print("    冷启动 UI 调用：", fake.calls, " chime 次数=", called["chime"])
    check(any(call[0] == "recording" and call[1] == "可以说话了"
              and call[2] == appmod.READY_BUBBLE_MS for call in fake.calls),
          "冷启动就绪 → 气泡「可以说话了」停留 %dms + 进入 recording"
          % appmod.READY_BUBBLE_MS)
    check(called["chime"] == 1, "冷启动就绪 → 播放 1 次轻提示音")
    check(app._cold_start_at is None, "就绪后冷启动计时已清零")
    app._cancel_timer()

    # 4c) 暖启动（无冷启动计时）→ 普通「请说话…」，不响
    called["chime"] = 0
    fake.calls.clear()
    app.state = "preparing"
    app._cold_start_at = None
    app._handle_event(("rec_started", 23456))
    print("    暖启动 UI 调用：", fake.calls, " chime 次数=", called["chime"])
    check(any(call[0] == "recording" and call[1] == "请说话…" for call in fake.calls),
          "暖启动 → 只显示「请说话…」气泡（不闪过场文案）")
    check(called["chime"] == 0, "暖启动 → 不播放提示音")
    app._cancel_timer()

    # 4d) UI 队列支持 bubble_ms
    indicator = ui_mod.Indicator(cfgmod.default_config())
    indicator.set_state("loading", "正在打开麦克风，请稍候…", 5000)
    item = indicator._queue.get_nowait()
    print("    UI 队列项：", item)
    check(item == ("state", "loading", "正在打开麦克风，请稍候…", 5000),
          "Indicator.set_state 携带 bubble_ms（冷启动提示可停留 5s）")
    indicator.set_state("idle")
    item2 = indicator._queue.get_nowait()
    check(item2 == ("state", "idle", "", None),
          "默认调用不受影响（bubble_ms=None → 默认 2s 淡出）")


def run_capture_wait_checks() -> None:
    banner("[5] capture 首批音频等待 200ms → 120ms")
    seconds = capture_mod.FIRST_AUDIO_WAIT_SECONDS
    need = int(16000 * 2 * seconds)
    print("    FIRST_AUDIO_WAIT_SECONDS=%.3f → 16000Hz/mono/s16 需 %d 字节（%.0fms 音频）"
          % (seconds, need, seconds * 1000.0))
    check(0.10 <= seconds <= 0.12,
          "首批音频等待 %.0fms 在 100~120ms（原 200ms，省 ~80ms 冷启动）" % (seconds * 1000.0))
    check(need == 3840, "起步音频门槛=%d 字节（120ms@16k mono s16）" % need)


def main() -> int:
    print("=" * 100)
    print("voice-ime 冷启动过场 / ready 提示音 / 性能优化 单元测试")
    print("=" * 100)
    run_chime_checks()
    run_threshold_checks()
    run_device_cache_checks()
    run_state_flow_checks()
    run_capture_wait_checks()
    print("=" * 100)
    print("检查项=%d，失败=%d，SKIP=%d" % (CHECKS, len(FAILURES), len(SKIPS)))
    for item in SKIPS:
        print("  SKIP: %s" % item)
    if FAILURES:
        print("结果：FAIL")
        for item in FAILURES:
            print("  - %s" % item)
        return 1
    print("结果：PASS（%d 项断言全部通过%s）" % (CHECKS, "；有 SKIP" if SKIPS else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
