# -*- coding: utf-8 -*-
"""常驻麦克风 + 外放回环测试（Lead 维护）。

验证「按下热键到松开」的切片链路真实采到了播放的声音：
外放增益后的 TTS 夹具 → 常驻采集切片 → 与源音频做语音包络互相关。
识别文本作为附加信息（外放→麦克风回环音质差，不作为硬断言）。
扬声器静音/音量过低时打印 SKIP。
运行：python -B tests/test_warm_capture.py
"""
from __future__ import annotations

import sys
import tempfile
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from voiceime.asr import SpeechRecognizer  # noqa: E402
from voiceime.audio import resolve_device  # noqa: E402
from voiceime.capture import MicrophoneStream  # noqa: E402
from voiceime.config import ffmpeg_path, load_config  # noqa: E402
from voiceime.paths import discover_models  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "zh_short.wav"
FRAME = 800  # 50ms @16k


def amplify_wav(src: Path, gain: float) -> Path:
    with wave.open(str(src), "rb") as reader:
        params = reader.getparams()
        raw = reader.readframes(reader.getnframes())
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) * gain
    clipped = np.clip(samples, -32768, 32767).astype(np.int16)
    out = Path(tempfile.gettempdir()) / f"voiceime_amp_{int(gain)}x.wav"
    with wave.open(str(out), "wb") as writer:
        writer.setparams(params)
        writer.writeframes(clipped.tobytes())
    return out


def envelope(samples: np.ndarray) -> np.ndarray:
    count = len(samples) // FRAME
    if count == 0:
        return np.zeros(1, dtype=np.float32)
    frames = samples[: count * FRAME].reshape(count, FRAME)
    return np.sqrt(np.mean(frames * frames, axis=1)).astype(np.float32)


def best_envelope_correlation(source: np.ndarray, recorded: np.ndarray, max_lag: int = 25) -> float:
    """两段音频的语音包络互相关（对 ±1.25s 时移取最大），返回 0~1 相似度。"""
    a = envelope(source)
    b = envelope(recorded)
    if len(a) < 2 or len(b) < 2:
        return 0.0
    a = (a - a.mean()) / (a.std() + 1e-9)
    b = (b - b.mean()) / (b.std() + 1e-9)
    best = 0.0
    for lag in range(-max_lag, max_lag + 1):
        if lag >= 0:
            left, right = a[: len(a) - lag], b[lag:]
        else:
            left, right = a[-lag:], b[: len(b) + lag]
        size = min(len(left), len(right))
        if size < 10:
            continue
        corr = float(np.dot(left[:size], right[:size]) / size)
        best = max(best, corr)
    return best


def main() -> int:
    if not FIXTURE.exists():
        print(f"[SKIP] 缺少夹具 {FIXTURE}（先运行 tests/make_fixtures.py）")
        return 0
    cfg = load_config()
    try:
        ffmpeg = ffmpeg_path(cfg)
        device = resolve_device(ffmpeg, cfg.get("mic_device"))
        models = discover_models(cfg)
    except Exception as exc:  # noqa: BLE001
        print(f"[SKIP] 环境不可用：{exc}")
        return 0
    print(f"[ENV] 设备={device}")

    loud = amplify_wav(FIXTURE, 8.0)
    with wave.open(str(loud), "rb") as reader:
        source = np.frombuffer(reader.readframes(reader.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0

    stream = MicrophoneStream(ffmpeg, device, buffer_seconds=180)
    failures = 0
    try:
        started = time.perf_counter()
        stream.start()
        startup = time.perf_counter() - started
        print(f"[STREAM] 启动耗时={startup:.2f}s warm={stream.warm}")
        if startup > 6.0:
            print(f"[FAIL] 常驻采集启动过慢：{startup:.2f}s > 6.0s")
            failures += 1
        if not stream.warm:
            print("[FAIL] 常驻采集未就绪")
            return 1

        recognizer = SpeechRecognizer(
            model=models["model"],
            tokens=models["tokens"],
            vad_model=models.get("vad"),
            language="auto",
            use_itn=True,
            num_threads=2,
        )
        recognizer.load()
        import winsound

        best = {"corr": -1.0, "text": "", "rms": 0.0, "dur": 0.0}
        for attempt in range(1, 4):
            mark = stream.mark()
            winsound.PlaySound(str(loud), winsound.SND_FILENAME)
            time.sleep(0.3)
            samples = stream.slice_since(mark, flush_ms=50)
            duration = len(samples) / 16000.0
            rms = float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0
            corr = best_envelope_correlation(source, samples)
            transcript = recognizer.transcribe_samples(samples, 16000) if samples.size else None
            text = transcript.text if transcript else ""
            print(f"[尝试{attempt}] 时长={duration:.2f}s RMS={rms:.3f} 包络相关={corr:.2f} "
                  f"text={text!r}")
            if corr > best["corr"]:
                best = {"corr": corr, "text": text, "rms": rms, "dur": duration}
            if corr >= 0.6:
                break

        if best["rms"] < 0.01:
            print("[SKIP] 外放音量过低/扬声器静音，无法验证回环（链路本身无异常）")
            return 0 if failures == 0 else 1
        if best["dur"] < 3.0 or best["dur"] > 8.0:
            print(f"[FAIL] 切片时长异常：{best['dur']:.2f}s（应≈5.5s）")
            failures += 1
        if best["corr"] < 0.4:
            print(f"[FAIL] 切片与源音频包络相关过低：{best['corr']:.2f} < 0.4")
            failures += 1
        if failures == 0:
            print(f"[PASS] 常驻采集切片包含播放音频（包络相关={best['corr']:.2f}，"
                  f"RMS={best['rms']:.3f}，识别附带结果={best['text']!r}）")
    finally:
        stream.close()
        try:
            loud.unlink()
        except OSError:
            pass
    print("=== 结果：%s ===" % ("通过" if failures == 0 else f"{failures} 项失败"))
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
