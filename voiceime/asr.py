# -*- coding: utf-8 -*-
"""voiceime.asr：本地 SenseVoice 语音识别（sherpa_onnx），Silero VAD 语音段检测。

对外接口（与 docs/DESIGN.md 一致）：
    @dataclass Transcript(text, audio_seconds, infer_seconds)
    class SpeechRecognizer(model, tokens, vad_model=None, language="auto",
                           use_itn=True, num_threads=2)
        load() / loaded / transcribe_wav(path, language=None)
        transcribe_samples(samples, sample_rate, language=None)

实现要点：
- 可直接复用本机已有的 SenseVoice ONNX 模型缓存（只读，不复制不修改）；
- 只要 VAD 模型可用，**每次识别都先用 Silero VAD 检测语音段、只解码语音段**，
  无任何语音段时直接返回空文本（避免底噪/呼吸声被 SenseVoice 幻觉成 "I." 之类的词）；
  首段起点前从整段采样回补 0.3s（onset padding），保护词首；
- VAD 不可用时回退整段解码（时长 > 28s 会打印告警）；
- 空音频 / 全静音返回空文本（不抛异常）；
- 16-bit PCM 之外的 WAV 直接拒绝；非 16k 输入用 numpy 线性重采样；
- 识别器按需惰性加载（load 幂等、线程安全）。
"""
from __future__ import annotations

import logging
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import sherpa_onnx

LOGGER = logging.getLogger(__name__)

SAMPLE_RATE = 16000
# 仅在 VAD 不可用、回退整段解码时作为告警阈值（SenseVoice 单段建议 <= 30s，留 2s 余量）
WHOLE_DECODE_MAX_SECONDS = 28.0
SUPPORTED_LANGUAGES = ("auto", "zh", "en", "yue", "ja", "ko")
# 这些语言词间无空格，VAD 分段结果直接拼接；英文需要空格分隔
_NO_SPACE_LANGUAGES = frozenset({"auto", "zh", "yue", "ja", "ko"})
# 静音判据（约 -80 dBFS）：仅数字零/近零触发，用于规避模型对纯静音的幻觉输出。
# 实测：2 秒全零样本直接解码会输出“그.”，因此必须显式短路为“”。
SILENCE_RMS_THRESHOLD = 1e-4

# Silero VAD 参数（见 docs/DESIGN.md）
VAD_THRESHOLD = 0.5
VAD_MIN_SILENCE = 0.5
VAD_MIN_SPEECH = 0.25
VAD_MAX_SPEECH = 30.0
VAD_WINDOW_SIZE = 512
VAD_BUFFER_SECONDS = 60
# 首段语音起点前回补的音频时长（0.3s，避免词首辅音被切掉）
VAD_ONSET_PADDING_SECONDS = 0.3


class AsrError(Exception):
    """语音识别失败（模型缺失、音频格式不支持、解码异常等，消息为中文可读提示）。"""


@dataclass
class Transcript:
    """一次识别的结果与耗时（秒）。"""

    text: str
    audio_seconds: float
    infer_seconds: float


def _check_language(language: str | None) -> str:
    """规范化并校验语言代码；未知语言抛 AsrError。"""
    value = (language or "auto").strip().lower()
    if value not in SUPPORTED_LANGUAGES:
        raise AsrError(
            f"不支持的语言：{language!r}。可选值：{'/'.join(SUPPORTED_LANGUAGES)}。"
        )
    return value


def _rms(samples: np.ndarray) -> float:
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))


def _resample_linear(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """numpy 线性插值重采样（单声道 float32）。"""
    if samples.size == 0 or src_rate == dst_rate:
        return samples
    new_length = max(1, int(round(samples.size * float(dst_rate) / float(src_rate))))
    if new_length == samples.size:
        return samples
    old_x = np.arange(samples.size, dtype=np.float64)
    new_x = np.linspace(0.0, samples.size - 1.0, new_length, dtype=np.float64)
    return np.interp(new_x, old_x, samples).astype(np.float32)


class SpeechRecognizer:
    """SenseVoice 离线识别器。

    ``language`` 在模型构建时确定；``transcribe_*`` 的 ``language`` 参数可临时覆盖：
    与当前识别器语言不同时会按需创建一个临时识别器（用完即释放，避免多份 228MB 模型常驻
    内存），所以频繁切换语言会有额外加载开销，常规使用建议在 config 中固定语言。
    """

    def __init__(
        self,
        model: str,
        tokens: str,
        vad_model: str | None = None,
        language: str = "auto",
        use_itn: bool = True,
        num_threads: int = 2,
    ) -> None:
        self.model = str(model)
        self.tokens = str(tokens)
        self.vad_model = str(vad_model) if vad_model else None
        self.language = _check_language(language)
        self.use_itn = bool(use_itn)
        self.num_threads = max(1, int(num_threads))
        self._recognizer: sherpa_onnx.OfflineRecognizer | None = None
        self._recognizer_language: str | None = None
        self._vad: sherpa_onnx.VoiceActivityDetector | None = None
        self._load_lock = threading.Lock()
        self._decode_lock = threading.Lock()
        # 最近一次识别过程的诊断信息（供 UI/测试展示 VAD 分段情况）
        self.last_used_vad = False
        self.last_segment_count = 0
        self.last_vad_seconds = 0.0

    # ------------------------------------------------------------------ 加载
    @property
    def loaded(self) -> bool:
        """模型是否已加载。"""
        return self._recognizer is not None

    def load(self) -> None:
        """加载 SenseVoice 模型（线程安全、幂等）。失败抛 AsrError。"""
        if self._recognizer is not None:
            return
        with self._load_lock:
            if self._recognizer is not None:
                return
            self._check_model_files()
            try:
                recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                    model=self.model,
                    tokens=self.tokens,
                    num_threads=self.num_threads,
                    use_itn=self.use_itn,
                    language=self.language,
                    debug=False,
                )
            except Exception as exc:  # noqa: BLE001 - 统一转成可读中文异常
                raise AsrError(f"加载 SenseVoice 模型失败：{exc}") from exc
            self._recognizer = recognizer
            self._recognizer_language = self.language
            self._prepare_vad()
            LOGGER.info(
                "SenseVoice 模型已加载：model=%s language=%s use_itn=%s threads=%d",
                self.model,
                self.language,
                self.use_itn,
                self.num_threads,
            )

    def _check_model_files(self) -> None:
        for value, label in ((self.model, "SenseVoice 模型文件"), (self.tokens, "tokens.txt")):
            if not Path(value).is_file():
                raise AsrError(
                    f"{label}不存在：{value}。请检查本机模型文件是否完整，"
                    "或在 config.json 的 model_dir / vad_model 中指定正确路径。"
                )

    def _prepare_vad(self) -> None:
        """VAD 不可用时只告警，长音频自动回退整段解码（不影响基础识别能力）。"""
        if not self.vad_model:
            return
        if not Path(self.vad_model).is_file():
            LOGGER.warning("VAD 模型文件不存在，长音频将回退整段解码：%s", self.vad_model)
            self.vad_model = None
            return
        try:
            self._vad = self._build_vad()
            LOGGER.info("Silero VAD 模型已加载：%s", self.vad_model)
        except Exception as exc:  # noqa: BLE001 - VAD 是增强能力，失败不阻塞识别
            LOGGER.warning("Silero VAD 模型加载失败，长音频将回退整段解码：%s", exc)
            self._vad = None

    def _build_vad(self) -> sherpa_onnx.VoiceActivityDetector:
        try:
            config = sherpa_onnx.VadModelConfig()
            config.silero_vad.model = self.vad_model
            config.silero_vad.threshold = VAD_THRESHOLD
            config.silero_vad.min_silence_duration = VAD_MIN_SILENCE
            config.silero_vad.min_speech_duration = VAD_MIN_SPEECH
            config.silero_vad.max_speech_duration = VAD_MAX_SPEECH
            config.silero_vad.window_size = VAD_WINDOW_SIZE
            config.sample_rate = SAMPLE_RATE
            config.num_threads = 1
            config.debug = False
            return sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=VAD_BUFFER_SECONDS)
        except Exception as exc:  # noqa: BLE001
            raise AsrError(f"加载 Silero VAD 模型失败：{exc}") from exc

    def _decoder_for(self, language: str) -> sherpa_onnx.OfflineRecognizer:
        """返回指定语言对应的识别器；同语言复用已加载实例，异语言按需临时创建。"""
        self.load()
        if language == self._recognizer_language:
            return self._recognizer
        LOGGER.info(
            "按需创建 language=%s 的临时 SenseVoice 识别器（仅本次调用使用）。", language
        )
        try:
            return sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=self.model,
                tokens=self.tokens,
                num_threads=self.num_threads,
                use_itn=self.use_itn,
                language=language,
                debug=False,
            )
        except Exception as exc:  # noqa: BLE001
            raise AsrError(f"创建 language={language} 的 SenseVoice 识别器失败：{exc}") from exc

    # ------------------------------------------------------------------ 识别
    def transcribe_wav(self, wav_path: str, language: str | None = None) -> Transcript:
        """识别 16-bit PCM WAV 文件；空文件返回空文本。"""
        path = Path(str(wav_path))
        if not path.is_file():
            raise AsrError(f"WAV 文件不存在：{path}")
        try:
            with wave.open(str(path), "rb") as wav:
                channels = wav.getnchannels()
                sampwidth = wav.getsampwidth()
                framerate = wav.getframerate()
                nframes = wav.getnframes()
                raw = wav.readframes(nframes)
        except (wave.Error, EOFError, OSError) as exc:
            raise AsrError(f"无法读取 WAV 文件（需要 16-bit PCM WAV）：{path}（{exc}）") from exc
        if sampwidth != 2:
            raise AsrError(
                f"仅支持 16-bit PCM WAV，当前位宽为 {sampwidth * 8} bit：{path}"
            )
        if nframes <= 0 or not raw:
            return Transcript(text="", audio_seconds=0.0, infer_seconds=0.0)
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if channels > 1:
            samples = samples.reshape(-1, channels).mean(axis=1).astype(np.float32)
        return self.transcribe_samples(samples, framerate, language=language)

    def transcribe_samples(
        self, samples, sample_rate: int, language: str | None = None
    ) -> Transcript:
        """识别 float32 单声道样本（[-1, 1]）；非 16k 输入自动线性重采样。

        VAD 模型可用时一律先做 Silero VAD 语音段检测：无语音段返回空文本（不返回幻觉文本），
        有语音段则只解码这些片段（首段前回补 0.3s onset padding）。
        """
        lang = _check_language(language or self.language)
        audio = self._prepare_samples(samples, sample_rate)
        audio_seconds = audio.size / float(SAMPLE_RATE)
        # 先清零诊断信息，保证早退路径（空音频/全静音）也不会残留上一次的 VAD 状态
        self.last_used_vad = False
        self.last_segment_count = 0
        self.last_vad_seconds = 0.0
        if audio.size == 0:
            return Transcript(text="", audio_seconds=0.0, infer_seconds=0.0)
        if _rms(audio) < SILENCE_RMS_THRESHOLD:
            LOGGER.debug("音频近似全静音（RMS < %.0e），直接返回空文本。", SILENCE_RMS_THRESHOLD)
            return Transcript(text="", audio_seconds=audio_seconds, infer_seconds=0.0)

        decoder = self._decoder_for(lang)
        use_vad = self._vad is not None
        started = time.perf_counter()
        with self._decode_lock:
            if use_vad:
                try:
                    text, segment_count, vad_seconds = self._decode_via_vad(audio, lang, decoder)
                    self.last_used_vad = True
                    self.last_segment_count = segment_count
                    self.last_vad_seconds = vad_seconds
                except Exception as exc:  # noqa: BLE001 - VAD 链路任何异常都回退整段解码
                    LOGGER.warning("VAD 分段识别失败，回退整段解码：%s", exc)
                    text = self._decode_whole(audio, decoder)
            else:
                if audio_seconds > WHOLE_DECODE_MAX_SECONDS:
                    LOGGER.warning(
                        "音频时长 %.1fs 超过 %.0fs 且无可用 VAD，回退整段解码。",
                        audio_seconds,
                        WHOLE_DECODE_MAX_SECONDS,
                    )
                text = self._decode_whole(audio, decoder)
        infer_seconds = time.perf_counter() - started
        return Transcript(text=text, audio_seconds=audio_seconds, infer_seconds=infer_seconds)

    # ------------------------------------------------------------------ 内部
    @staticmethod
    def _prepare_samples(samples, sample_rate: int) -> np.ndarray:
        try:
            rate = int(sample_rate)
        except (TypeError, ValueError) as exc:
            raise AsrError(f"采样率非法：{sample_rate!r}（需要正整数）。") from exc
        if rate <= 0:
            raise AsrError(f"采样率非法：{rate}（需要正整数）。")
        try:
            audio = np.asarray(samples, dtype=np.float32)
        except (TypeError, ValueError) as exc:
            raise AsrError(f"音频样本无法转换为 float32 数组：{exc}") from exc
        audio = np.nan_to_num(audio, nan=0.0, posinf=0.0, neginf=0.0)
        if audio.ndim == 2:
            audio = audio.mean(axis=1).astype(np.float32)
        elif audio.ndim > 2:
            audio = audio.reshape(-1)
        np.clip(audio, -1.0, 1.0, out=audio)
        if audio.size and rate != SAMPLE_RATE:
            audio = _resample_linear(audio, rate, SAMPLE_RATE)
        return np.ascontiguousarray(audio, dtype=np.float32)

    def _decode_whole(self, samples: np.ndarray, decoder: sherpa_onnx.OfflineRecognizer) -> str:
        try:
            stream = decoder.create_stream()
            stream.accept_waveform(SAMPLE_RATE, samples)
            decoder.decode_stream(stream)
            text = stream.result.text
        except Exception as exc:  # noqa: BLE001
            raise AsrError(f"SenseVoice 整段识别失败：{exc}") from exc
        return (text or "").strip()

    def _decode_via_vad(
        self, samples: np.ndarray, language: str, decoder: sherpa_onnx.OfflineRecognizer
    ) -> tuple[str, int, float]:
        """用 Silero VAD 检测语音段后逐段识别，返回 (拼接文本, 段数, VAD 耗时)。

        无任何语音段时返回 ("", 0, vad_seconds)——绝不对底噪做整段解码（否则 SenseVoice 会幻觉出
        "I." 之类的文本并被注入焦点窗口）。首段起点前从整段采样回补 0.3s onset padding。
        """
        vad = self._vad
        if vad is None:
            raise AsrError("VAD 模型不可用，无法分段识别。")
        started = time.perf_counter()
        try:
            vad.reset()
            window = VAD_WINDOW_SIZE
            for offset in range(0, samples.size, window):
                chunk = samples[offset : offset + window]
                if chunk.size < window:
                    chunk = np.pad(chunk, (0, window - chunk.size))
                vad.accept_waveform(chunk)
            vad.flush()
            segments: list[tuple[int, np.ndarray]] = []
            while not vad.empty():
                segment = vad.front
                segment_start = int(segment.start)
                segment_samples = np.asarray(segment.samples, dtype=np.float32)
                vad.pop()
                if segment_samples.size:
                    segments.append((segment_start, segment_samples))
        except Exception as exc:  # noqa: BLE001
            raise AsrError(f"VAD 分段失败：{exc}") from exc
        vad_seconds = time.perf_counter() - started

        if not segments:
            LOGGER.info(
                "VAD 未检测到语音段（audio=%.2fs），返回空文本。", samples.size / float(SAMPLE_RATE)
            )
            return "", 0, vad_seconds

        # 首段前回补 onset padding（0.3s）：从整段采样截取首段起点之前的
        # 音频拼到首段前面，避免词首辅音/气息被 VAD 切掉导致识别缺字。
        padding_samples = int(round(VAD_ONSET_PADDING_SECONDS * SAMPLE_RATE))
        first_start, first_segment = segments[0]
        pad_begin = max(0, first_start - padding_samples)
        if pad_begin < first_start:
            onset = np.asarray(samples[pad_begin:first_start], dtype=np.float32)
            if onset.size:
                segments[0] = (pad_begin, np.concatenate([onset, first_segment]))

        texts: list[str] = []
        for _segment_start, segment_samples in segments:
            if _rms(segment_samples) < SILENCE_RMS_THRESHOLD:
                continue
            segment_text = self._decode_whole(segment_samples, decoder)
            if segment_text:
                texts.append(segment_text)
        separator = "" if language in _NO_SPACE_LANGUAGES else " "
        text = separator.join(texts).strip()
        LOGGER.info(
            "VAD 分段识别：audio=%.2fs -> %d 段（解码 %d 段），VAD 耗时 %.3fs。",
            samples.size / float(SAMPLE_RATE),
            len(segments),
            len(texts),
            vad_seconds,
        )
        return text, len(segments), vad_seconds
