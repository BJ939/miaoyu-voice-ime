# -*- coding: utf-8 -*-
"""音频采集模块测试：真实麦克风录 3 秒 + WAV 合法性 / RMS 校验。

运行（在工程根目录下）::

    python -B tests/test_audio.py

- 真实麦克风不可用（无 dshow 设备 / 被占用 / 无权限）时打印 SKIP 并 exit 0，
  绝不以 PASS 冒充（单元检查仍会执行并如实汇报）；
- 打印实际设备名、录音耗时、WAV 参数、帧数、RMS/峰值（含 dBFS）。
"""
from __future__ import annotations

import math
import shutil
import sys
import tempfile
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _path in (str(ROOT),):
    if _path not in sys.path:
        sys.path.insert(0, _path)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import numpy as np

from voiceime.audio import AudioError, AudioRecorder, list_input_devices, resolve_device
from voiceime.config import ffmpeg_path, load_config

FAILURES: list[str] = []
CHECK_COUNT = 0
RECORD_SECONDS = 3.0


def check(condition: bool, message: str) -> bool:
    global CHECK_COUNT
    CHECK_COUNT += 1
    if condition:
        print("    [OK] %s" % message)
        return True
    print("    [FAIL] %s" % message)
    FAILURES.append(message)
    return False


def inspect_wav(path: str) -> dict:
    """读取 WAV 参数、帧数据与 RMS/峰值。"""
    with wave.open(str(path), "rb") as wav:
        info = {
            "rate": wav.getframerate(),
            "channels": wav.getnchannels(),
            "sampwidth": wav.getsampwidth(),
            "frames": wav.getnframes(),
            "raw": wav.readframes(wav.getnframes()),
        }
    samples = np.frombuffer(info["raw"], dtype=np.int16)
    info["duration"] = info["frames"] / float(info["rate"]) if info["rate"] else 0.0
    if samples.size:
        squared = np.square(samples.astype(np.float64))
        info["rms"] = float(np.sqrt(squared.mean()))
        info["peak"] = int(np.abs(samples).max())
    else:
        info["rms"] = 0.0
        info["peak"] = 0
    info["dbfs"] = 20.0 * math.log10(info["rms"] / 32768.0) if info["rms"] > 0 else float("-inf")
    info["size"] = Path(path).stat().st_size
    info["path"] = path
    return info


def run_unit_checks(ffmpeg: str, workdir: Path) -> None:
    """不依赖麦克风真实采集的单元检查。"""
    print("-" * 100)
    print("[UNIT] 设备枚举/录音器状态机的错误路径检查（不需要真实采集）")
    try:
        list_input_devices(r"C:\不存在的目录\ffmpeg.exe")
        check(False, "非法 ffmpeg 路径应抛 AudioError 但未抛出")
    except AudioError as exc:
        check("ffmpeg" in str(exc), "非法 ffmpeg 路径抛 AudioError：%s" % str(exc)[:120])

    try:
        AudioRecorder(ffmpeg, sample_rate=0)
        check(False, "sample_rate=0 应抛 AudioError 但未抛出")
    except AudioError as exc:
        check("参数非法" in str(exc), "非法 sample_rate 抛 AudioError：%s" % exc)

    recorder = AudioRecorder(ffmpeg, out_dir=str(workdir / "unit"))
    check(recorder.active is False, "未 start() 时 active=False")
    check(recorder.elapsed() == 0.0, "未 start() 时 elapsed()=0.0")
    try:
        recorder.stop()
        check(False, "未 start() 就 stop() 应抛 AudioError 但未抛出")
    except AudioError as exc:
        check("没有正在进行" in str(exc), "未 start() 就 stop() 抛 AudioError：%s" % exc)
    check(recorder.active is False, "stop() 异常后 active 仍为 False")


def run_microphone_checks(ffmpeg: str, cfg: dict, workdir: Path) -> str | None:
    """真实麦克风录 3 秒并校验；返回 None 表示已验证，返回字符串表示 SKIP 原因。"""
    print("-" * 100)
    print("[MIC] 枚举 dshow 音频输入设备")
    try:
        devices = list_input_devices(ffmpeg)
    except AudioError as exc:
        print("[SKIP] 无法枚举麦克风设备：%s" % exc)
        return "无法枚举麦克风设备"
    print("[DEVICES] 共 %d 个音频输入设备：" % len(devices))
    for index, name in enumerate(devices):
        print("  [%d] %s" % (index, name))
    if not devices:
        print("[SKIP] dshow 未检测到任何音频输入设备（麦克风未连接/被禁用/隐私权限未开）。")
        return "没有可用的 dshow 音频输入设备"

    configured = cfg.get("mic_device")
    try:
        device = resolve_device(ffmpeg, configured)
        fallback = resolve_device(ffmpeg, "voiceime_不存在的设备_用于测试回退")
    except AudioError as exc:
        print("[SKIP] 无法选择麦克风设备：%s" % exc)
        return "无法选择麦克风设备"
    print("[DEVICE] config.mic_device=%r -> 实际选用 %r" % (configured, device))
    check(configured is None or device == configured, "resolve_device 在配置有效时返回配置设备")
    check(fallback == devices[0], "resolve_device 在配置名无效时回退到第一个设备")

    record_dir = workdir / "record"
    recorder = AudioRecorder(ffmpeg, device=device, out_dir=str(record_dir))
    start_t0 = time.perf_counter()
    try:
        recorder.start()
    except AudioError as exc:
        print("[SKIP] 麦克风无法开始录音：%s" % exc)
        return "麦克风无法开始录音"
    start_seconds = time.perf_counter() - start_t0
    print("[REC] start() 返回耗时 %.3fs，active=%s，elapsed=%.3fs" % (start_seconds, recorder.active, recorder.elapsed()))
    check(recorder.active is True, "start() 后 active=True")

    time.sleep(RECORD_SECONDS)
    mid_elapsed = recorder.elapsed()
    print("[REC] 录音中：elapsed()=%.3fs（实际 sleep %.1fs），active=%s" % (mid_elapsed, RECORD_SECONDS, recorder.active))
    check(RECORD_SECONDS - 0.3 <= mid_elapsed <= RECORD_SECONDS + 1.0, "录音中 elapsed()=%.3fs ≈ %.1fs" % (mid_elapsed, RECORD_SECONDS))

    stop_t0 = time.perf_counter()
    try:
        wav_path = recorder.stop()
    except AudioError as exc:
        check(False, "麦克风已开始录音但 stop() 失败：%s" % exc)
        return None
    stop_seconds = time.perf_counter() - stop_t0
    print("[REC] stop() 返回耗时 %.3fs，active=%s，elapsed()=%.3fs" % (stop_seconds, recorder.active, recorder.elapsed()))
    print("[REC] WAV 路径：%s" % wav_path)
    check(recorder.active is False, "stop() 后 active=False")
    check(Path(wav_path).is_absolute(), "stop() 返回绝对路径")
    after_stop_elapsed = recorder.elapsed()
    check(
        RECORD_SECONDS - 0.3 <= after_stop_elapsed <= RECORD_SECONDS + 2.0,
        "stop() 后 elapsed() 冻结为 %.3fs ≈ %.1fs" % (after_stop_elapsed, RECORD_SECONDS),
    )

    info = inspect_wav(wav_path)
    print(
        "[WAV] rate=%d Hz  channels=%d  width=%d bit  frames=%d  duration=%.3fs  size=%d 字节"
        % (
            info["rate"],
            info["channels"],
            info["sampwidth"] * 8,
            info["frames"],
            info["duration"],
            info["size"],
        )
    )
    print("[WAV] RMS=%.2f（%.1f dBFS）  峰值=%d" % (info["rms"], info["dbfs"], info["peak"]))
    check(info["rate"] == 16000, "WAV 采样率 16000Hz")
    check(info["channels"] == 1, "WAV 单声道")
    check(info["sampwidth"] == 2, "WAV 16-bit PCM")
    check(info["frames"] > 16000, "WAV 帧数 %d > 16000（>1 秒）" % info["frames"])
    check(2.0 <= info["duration"] <= 6.0, "WAV 时长 %.3fs 在 2~6 秒（约 3 秒录音）" % info["duration"])
    check(info["peak"] > 0, "录音非全零（峰值 %d）" % info["peak"])
    check(info["rms"] > 0.0, "录音 RMS=%.2f > 0" % info["rms"])

    # cancel 路径：录 1 秒后取消，文件必须被删除
    cancel_dir = workdir / "cancel"
    recorder2 = AudioRecorder(ffmpeg, device=device, out_dir=str(cancel_dir))
    try:
        recorder2.start()
    except AudioError as exc:
        print("[SKIP] cancel 子用例无法开始录音：%s" % exc)
        return None
    time.sleep(1.0)
    cancel_t0 = time.perf_counter()
    recorder2.cancel()
    print("[CANCEL] cancel() 耗时 %.3fs" % (time.perf_counter() - cancel_t0))
    check(recorder2.active is False, "cancel() 后 active=False")
    check(recorder2.elapsed() == 0.0, "cancel() 后 elapsed()=0.0")
    leftovers = sorted(str(p) for p in cancel_dir.glob("rec_*.wav"))
    check(not leftovers, "cancel() 删除临时文件（剩余 %r）" % leftovers)
    return None


def main() -> int:
    started_all = time.perf_counter()
    print("=" * 100)
    print("[ENV] python=%s  numpy=%s" % (sys.version.split()[0], np.__version__))
    cfg = load_config()
    try:
        ffmpeg = ffmpeg_path(cfg)
    except Exception as exc:  # noqa: BLE001
        print("[SKIP] 无法定位 ffmpeg，音频采集无法验证：%s" % exc)
        print("[SUMMARY] SKIP（未验证任何真实录音，不是 PASS）")
        return 0
    print("[ENV] ffmpeg=%s" % ffmpeg)

    workdir = Path(tempfile.mkdtemp(prefix="voiceime_audio_test_"))
    skip_reason: str | None = None
    try:
        run_unit_checks(ffmpeg, workdir)
        skip_reason = run_microphone_checks(ffmpeg, cfg, workdir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    elapsed = time.perf_counter() - started_all
    print("=" * 100)
    if FAILURES:
        print("[SUMMARY] 检查 %d 项，失败 %d 项，总耗时 %.2fs" % (CHECK_COUNT, len(FAILURES), elapsed))
        for message in FAILURES:
            print("  [FAIL] %s" % message)
        return 1
    if skip_reason is not None:
        print(
            "[SUMMARY] 单元检查 %d 项通过；真实麦克风录音 SKIP：%s（未验证真实录音，不是 PASS）"
            % (CHECK_COUNT, skip_reason)
        )
        return 0
    print("[SUMMARY] 全部 %d 项检查通过（含真实麦克风 3 秒录音），总耗时 %.2fs" % (CHECK_COUNT, elapsed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
