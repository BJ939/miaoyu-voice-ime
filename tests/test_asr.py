# -*- coding: utf-8 -*-
"""ASR 模块测试：SAPI 合成中文/英文夹具 → 本地 SenseVoice 识别断言 + 实测数字打印。

运行（在工程根目录下）::

    python -B tests/test_asr.py

覆盖点（VAD 模型可用时**每次识别都先过 Silero VAD**，只解码语音段）：
1. 中文短句（auto）走 VAD 路径识别含“项目周报”“开(个)会”，打印 model_load/audio/infer/RTF 并断言 RTF<0.5；
2. 按调用切换语言（transcribe_wav(..., language="zh")）走临时识别器路径；
3. 英文短句用 language="en" 独立识别器识别（hello/test），断言 RTF<0.5；
4. 62.6 秒长音频（zh_long.wav）走 Silero VAD 分段，断言确实使用了 VAD、分段数 ≥2；
5. 非 16k 输入（32k 上采样）自动重采样后仍能识别；
6. 空音频 / 全静音返回空文本不抛异常；**2 秒低幅底噪（RMS≈0.005）VAD 判定无语音 → 空文本，
   不得返回 "I." 之类幻觉文本（回归用例）**；8-bit WAV 拒绝；不存在的文件给出中文异常；
7. 无 VAD 时长音频回退整段解码不崩。
"""
from __future__ import annotations

import gc
import re
import sys
import tempfile
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
for _path in (str(ROOT), str(TESTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
# 库模块的 logging 走 stderr；统一 UTF-8，避免终端把中文告警显示成乱码
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import sherpa_onnx

import make_fixtures
from voiceime.asr import AsrError, SpeechRecognizer, Transcript
from voiceime.config import ffmpeg_path, load_config
from voiceime.paths import discover_models

FAILURES: list[str] = []
CHECK_COUNT = 0


def check(condition: bool, message: str) -> bool:
    global CHECK_COUNT
    CHECK_COUNT += 1
    if condition:
        print("    [OK] %s" % message)
        return True
    print("    [FAIL] %s" % message)
    FAILURES.append(message)
    return False


def print_case(
    name: str,
    transcript: Transcript,
    language: str,
    model_load_seconds: float,
    used_vad: bool,
    segment_count: int,
) -> float:
    rtf = (
        transcript.infer_seconds / transcript.audio_seconds
        if transcript.audio_seconds > 0
        else 0.0
    )
    print(
        "[CASE] %-14s lang=%-4s model_load=%.3fs audio=%7.3fs infer=%7.3fs RTF=%7.4f  vad=%-3s segs=%d"
        % (
            name,
            language,
            model_load_seconds,
            transcript.audio_seconds,
            transcript.infer_seconds,
            rtf,
            "yes" if used_vad else "no",
            segment_count,
        )
    )
    print("       text=%r" % transcript.text)
    return rtf


def read_wav_samples(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as wav:
        rate = wav.getframerate()
        channels = wav.getnchannels()
        frames = wav.getnframes()
        raw = wav.readframes(frames)
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1).astype(np.float32)
    return samples, rate


def write_test_wav(path: Path, sample_width: int, rate: int, frames: int) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(sample_width)
        wav.setframerate(rate)
        wav.writeframes(b"\x00" * (frames * sample_width))


def main() -> int:
    started_all = time.perf_counter()
    print("=" * 100)
    print("[ENV] python=%s  sherpa_onnx=%s  numpy=%s" % (sys.version.split()[0], sherpa_onnx.__version__, np.__version__))

    cfg = load_config()
    try:
        models = discover_models(cfg)
        ffmpeg = ffmpeg_path(cfg)
    except Exception as exc:  # noqa: BLE001
        print("[FAIL] 模型/ffmpeg 定位失败：%s" % exc)
        return 1
    print("[ENV] ffmpeg=%s" % ffmpeg)
    print(
        "[MODEL] sensevoice=%s (%.1f MB)"
        % (models["model"], Path(models["model"]).stat().st_size / 1024.0 / 1024.0)
    )
    print("[MODEL] tokens=%s" % models["tokens"])
    print("[MODEL] vad=%s" % (models["vad"] or "（无）"))
    if not models["vad"]:
        print("[FAIL] 未找到 Silero VAD 模型，长音频分段路径无法验证。")
        return 1

    try:
        fixtures = make_fixtures.ensure_fixtures(verbose=True)
    except make_fixtures.FixtureError as exc:
        print("[FAIL] 测试音频夹具生成失败：%s" % exc)
        return 1

    # ---------------------------------------------------------------- 基础加载
    print("-" * 100)
    recognizer = SpeechRecognizer(
        model=models["model"],
        tokens=models["tokens"],
        vad_model=models["vad"],
        language="auto",
        use_itn=True,
        num_threads=2,
    )
    check(recognizer.loaded is False, "load() 之前 loaded=False")
    load_t0 = time.perf_counter()
    recognizer.load()
    first_load = time.perf_counter() - load_t0
    load_t0 = time.perf_counter()
    recognizer.load()
    second_load = time.perf_counter() - load_t0
    check(recognizer.loaded is True, "load() 之后 loaded=True")
    check(second_load < 0.001, "load() 幂等：第二次耗时 %.4fs < 0.001s" % second_load)
    print("[LOAD] SenseVoice 首次加载 %.3fs，二次调用 %.4fs" % (first_load, second_load))

    # ---------------------------------------------------------------- 用例 1：中文短句 auto
    print("-" * 100)
    print("[CASE] zh_short.wav  language=auto（VAD 判定 + 语音段解码；短音频不再整段解码）")
    transcript = recognizer.transcribe_wav(str(fixtures["zh_short"]), language="auto")
    rtf = print_case("zh_short", transcript, "auto", 0.0, recognizer.last_used_vad, recognizer.last_segment_count)
    check("项目周报" in transcript.text, "中文短句文本含“项目周报”")
    check(
        re.search(r"开.{0,2}会", transcript.text) is not None,
        "中文短句文本含“开(个)会”（SAPI 原文“开个会”，实测可能识别为“开个会”）",
    )
    check(0.0 < transcript.audio_seconds < 10.0, "中文短句 audio_seconds=%.3fs 在 (0,10) 内" % transcript.audio_seconds)
    check(rtf < 0.5, "中文短句 RTF=%.4f < 0.5" % rtf)
    check(recognizer.last_used_vad is True, "中文短句（5s）也走 VAD 判定路径（last_used_vad=True）")
    check(recognizer.last_segment_count >= 1, "中文短句 VAD 语音段数 %d ≥ 1" % recognizer.last_segment_count)

    # ---------------------------------------------------------------- 用例 2：按调用切换 language=zh
    print("-" * 100)
    print("[CASE] zh_short.wav  language=zh（与识别器语言 auto 不同：按需临时识别器，其加载耗时不计入 infer_seconds）")
    transcript = recognizer.transcribe_wav(str(fixtures["zh_short"]), language="zh")
    rtf = print_case("zh_short_ovrd", transcript, "zh", 0.0, recognizer.last_used_vad, recognizer.last_segment_count)
    check("项目周报" in transcript.text, "language=zh 覆盖路径文本含“项目周报”")
    check(rtf < 0.5, "language=zh 覆盖路径 RTF=%.4f < 0.5" % rtf)

    # ---------------------------------------------------------------- 用例 3：英文 language=en
    print("-" * 100)
    print("[CASE] en_short.wav  language=en（独立识别器，单独计量模型加载）")
    recognizer_en = SpeechRecognizer(
        model=models["model"],
        tokens=models["tokens"],
        vad_model=models["vad"],
        language="en",
        use_itn=True,
        num_threads=2,
    )
    load_t0 = time.perf_counter()
    recognizer_en.load()
    en_load = time.perf_counter() - load_t0
    transcript = recognizer_en.transcribe_wav(str(fixtures["en_short"]), language="en")
    rtf = print_case("en_short", transcript, "en", en_load, recognizer_en.last_used_vad, recognizer_en.last_segment_count)
    lower_text = transcript.text.lower()
    check(
        ("hello" in lower_text) or ("test" in lower_text),
        "英文短句文本含 hello 或 test（实际 %r）" % transcript.text,
    )
    check(rtf < 0.5, "英文短句 RTF=%.4f < 0.5" % rtf)
    del recognizer_en
    gc.collect()

    # ---------------------------------------------------------------- 用例 4：长音频 VAD 分段
    print("-" * 100)
    print("[CASE] zh_long.wav  language=auto（62.60s 长音频：Silero VAD 多段路径）")
    long_seconds = fixture_seconds(fixtures["zh_long"])
    transcript = recognizer.transcribe_wav(str(fixtures["zh_long"]), language="auto")
    rtf = print_case("zh_long", transcript, "auto", 0.0, recognizer.last_used_vad, recognizer.last_segment_count)
    check(long_seconds >= 35.0, "zh_long.wav 时长 %.2fs ≥ 35s（覆盖 VAD 分段路径）" % long_seconds)
    check(transcript.audio_seconds >= 35.0, "长音频 audio_seconds=%.2fs ≥ 35s" % transcript.audio_seconds)
    check(recognizer.last_used_vad is True, "长音频确实走了 VAD 分段路径（last_used_vad=True）")
    check(recognizer.last_segment_count >= 2, "VAD 分段数 %d ≥ 2" % recognizer.last_segment_count)
    check(
        recognizer.last_vad_seconds < 5.0,
        "VAD 切段耗时 %.3fs < 5s（不含解码，仅 VAD 前向）" % recognizer.last_vad_seconds,
    )
    check("项目周报" in transcript.text, "长音频识别文本含“项目周报”（多段拼接正确）")
    check(len(transcript.text) >= 80, "长音频识别文本长度 %d ≥ 80（多段拼接非截断）" % len(transcript.text))
    check(rtf < 0.5, "长音频 RTF=%.4f < 0.5" % rtf)

    # ---------------------------------------------------------------- 用例 5：非 16k 输入重采样
    print("-" * 100)
    print("[CASE] zh_short.wav 上采样到 32kHz 后识别（验证 transcribe_samples 非 16k 重采样路径）")
    samples, rate = read_wav_samples(fixtures["zh_short"])
    upsampled = np.repeat(samples, 2).astype(np.float32)
    transcript = recognizer.transcribe_samples(upsampled, rate * 2, language="auto")
    rtf = print_case("resample_32k", transcript, "auto", 0.0, recognizer.last_used_vad, recognizer.last_segment_count)
    check(rate == 16000, "原始夹具采样率 16000Hz")
    check("项目周报" in transcript.text, "32kHz 输入重采样后文本含“项目周报”")
    check(rtf < 0.5, "重采样路径 RTF=%.4f < 0.5" % rtf)

    # ---------------------------------------------------------------- 用例 6：空音频 / 全静音
    print("-" * 100)
    print("[CASE] 空音频与全静音：必须返回空文本且不抛异常")
    empty = recognizer.transcribe_samples(np.zeros(0, dtype=np.float32), 16000)
    check(isinstance(empty, Transcript), "空音频返回 Transcript 对象")
    check(empty.text == "", "空音频 text==''（实际 %r）" % empty.text)
    check(empty.audio_seconds == 0.0, "空音频 audio_seconds=0.0")

    silence = recognizer.transcribe_samples(np.zeros(16000 * 2, dtype=np.float32), 16000)
    check(isinstance(silence, Transcript), "全静音返回 Transcript 对象")
    check(silence.text == "", "全静音（2s 数字零）text==''（实际 %r）" % silence.text)
    print(
        "[SILENCE] 2.0s 全零样本 -> text=%r audio=%.2fs infer=%.4fs"
        % (silence.text, silence.audio_seconds, silence.infer_seconds)
    )

    # ---------------------------------------------------------------- 用例 6b：底噪不得产生幻觉文本（回归）
    print("-" * 100)
    print("[CASE] 2.0s 低幅底噪（RMS≈0.005）：VAD 判定无语音 -> 必须返回空文本")
    rng = np.random.default_rng(20261027)
    noise = (rng.standard_normal(16000 * 2) * 0.005).astype(np.float32)
    np.clip(noise, -1.0, 1.0, out=noise)
    noise_rms = float(np.sqrt(np.mean(np.square(noise.astype(np.float64)))))
    print(
        "[NOISE] dur=2.00s rms=%.5f（修复前同幅底噪被整段解码幻觉成 '그.'，app 集成实测真实底噪 1.90s 输出 'I.'）"
        % noise_rms
    )
    transcript = recognizer.transcribe_samples(noise, 16000, language="auto")
    rtf = print_case("noise_2s", transcript, "auto", 0.0, recognizer.last_used_vad, recognizer.last_segment_count)
    check(abs(noise_rms - 0.005) < 0.001, "底噪 RMS=%.5f ≈ 0.005" % noise_rms)
    check(transcript.text == "", "2s 底噪识别 text==''（实际 %r）" % transcript.text)
    check(recognizer.last_used_vad is True, "底噪用例走了 VAD 判定路径（last_used_vad=True）")
    check(recognizer.last_segment_count == 0, "底噪用例 VAD 语音段数 0（无语音段）")
    check(rtf < 0.5, "底噪用例 RTF=%.4f < 0.5" % rtf)

    with tempfile.TemporaryDirectory(prefix="voiceime_asr_test_") as tmpdir:
        empty_wav = Path(tmpdir) / "empty.wav"
        write_test_wav(empty_wav, sample_width=2, rate=16000, frames=0)
        empty_result = recognizer.transcribe_wav(str(empty_wav))
        check(empty_result.text == "", "0 帧 16-bit WAV text==''（实际 %r）" % empty_result.text)

        # ------------------------------------------------------------ 用例 7：8-bit WAV 必须拒绝
        print("-" * 100)
        print("[CASE] 8-bit WAV：必须抛出可读中文 AsrError")
        wav8 = Path(tmpdir) / "eight_bit.wav"
        write_test_wav(wav8, sample_width=1, rate=16000, frames=16000)
        try:
            recognizer.transcribe_wav(str(wav8))
            check(False, "8-bit WAV 应抛 AsrError 但未抛出")
        except AsrError as exc:
            check("16-bit" in str(exc), "8-bit WAV 抛 AsrError 且提示 16-bit：%s" % exc)

        # ------------------------------------------------------------ 用例 8：不存在的文件
        print("-" * 100)
        print("[CASE] 不存在的 WAV 与不支持的语言：必须抛出中文 AsrError")
        missing_path = Path(tmpdir) / "no_such_file.wav"
        try:
            recognizer.transcribe_wav(str(missing_path))
            check(False, "不存在的 WAV 应抛 AsrError 但未抛出")
        except AsrError as exc:
            check("不存在" in str(exc), "不存在的 WAV 抛 AsrError（中文提示）：%s" % exc)
        try:
            recognizer.transcribe_samples(np.zeros(1600, dtype=np.float32), 16000, language="fr")
            check(False, "不支持的语言 fr 应抛 AsrError 但未抛出")
        except AsrError as exc:
            check("不支持的语言" in str(exc), "不支持的语言抛 AsrError（中文提示）：%s" % exc)

    # ---------------------------------------------------------------- 用例 9：无 VAD 回退整段解码
    print("-" * 100)
    print("[CASE] >28s 且无 VAD 模型：回退整段解码不应崩溃")
    recognizer_no_vad = SpeechRecognizer(
        model=models["model"],
        tokens=models["tokens"],
        vad_model=None,
        language="auto",
        use_itn=True,
        num_threads=2,
    )
    load_t0 = time.perf_counter()
    recognizer_no_vad.load()
    no_vad_load = time.perf_counter() - load_t0
    long_samples, long_rate = read_wav_samples(fixtures["zh_long"])
    clip = long_samples[: int(30 * long_rate)]
    transcript = recognizer_no_vad.transcribe_samples(clip, long_rate, language="auto")
    rtf = print_case("fallback_30s", transcript, "auto", no_vad_load, recognizer_no_vad.last_used_vad, recognizer_no_vad.last_segment_count)
    check(recognizer_no_vad.last_used_vad is False, "无 VAD 时 last_used_vad=False（走回退整段解码）")
    check(transcript.audio_seconds >= 29.0, "回退用例 audio_seconds=%.2fs（30s 截取）" % transcript.audio_seconds)
    check(len(transcript.text) > 0, "回退整段解码得到非空文本（长度 %d）" % len(transcript.text))
    check(rtf < 0.5, "回退整段解码 RTF=%.4f < 0.5" % rtf)
    del recognizer_no_vad
    gc.collect()

    # ---------------------------------------------------------------- 汇总
    elapsed = time.perf_counter() - started_all
    print("=" * 100)
    if FAILURES:
        print("[SUMMARY] 检查 %d 项，失败 %d 项，总耗时 %.2fs" % (CHECK_COUNT, len(FAILURES), elapsed))
        for message in FAILURES:
            print("  [FAIL] %s" % message)
        return 1
    print("[SUMMARY] 全部 %d 项检查通过，总耗时 %.2fs" % (CHECK_COUNT, elapsed))
    return 0


def fixture_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as wav:
        return wav.getnframes() / float(wav.getframerate())


if __name__ == "__main__":
    raise SystemExit(main())
