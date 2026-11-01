# -*- coding: utf-8 -*-
"""voice-ime 应用入口。

状态机（全部事件在单一控制线程里串行处理）：
    idle --热键--> recording --热键/超时--> transcribing --成功--> 注入文本 --> idle
                       |--Esc--> idle
使用方式：
    python app.py                   正常启动（悬浮球 + 全局热键）
    python app.py --check           环境自检（模型/ffmpeg/麦克风）
    python app.py --file a.wav      离线识别一个 WAV 文件（调试用）
    python app.py --file a.wav --insert   识别后直接注入焦点窗口
    python app.py --quit            通知正在运行的实例退出
    python app.py --no-ui           不显示悬浮球（只保留热键）
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import queue
import sys
import threading
import time
import traceback
from pathlib import Path

from voiceime import config as cfgmod
from voiceime.paths import ModelMissingError, discover_models

LOG = logging.getLogger("voice-ime")

# ---------------------------------------------------------------------------
# 冷启动过场（麦克风空闲释放后重新开麦 / 首次加载模型）
# ---------------------------------------------------------------------------
# 冷启动过渡超过该阈值才给「可以说话了」就绪信号（视觉气泡 + 轻提示音）；
# 更快的暖启动（常驻麦克风还热着，toggle 后 ~0ms 进录音）不弹过场、不响。
READY_MIN_MS = 300.0
# 就绪气泡「可以说话了」停留时长（之后淡出；视觉总时长约 0.8~1.1s）
READY_BUBBLE_MS = 800
# 冷启动提示气泡的兜底时长（正常会在就绪时被替换成 ready 气泡）
LOADING_BUBBLE_MS = 5000
# ready 提示音：内存生成，避免用系统提示音（音量不可控、太吵）
CHIME_FREQ_HZ = 880.0
CHIME_DURATION_MS = 75.0
CHIME_AMPLITUDE = 0.10  # 满幅 10%（≈-20dBFS，很轻）
_CHIME_WAV_CACHE: bytes | None = None


def _should_signal_ready(elapsed_ms: float) -> bool:
    """冷启动耗时超过 READY_MIN_MS 才给 ready 视觉+听觉信号（快路径保持安静）。"""
    return float(elapsed_ms) > READY_MIN_MS


def _ready_chime_wav() -> bytes:
    """内存生成极轻的 ready 提示音 WAV（约 75ms 880Hz 正弦，带 12ms 淡入淡出）。"""
    global _CHIME_WAV_CACHE
    if _CHIME_WAV_CACHE is not None:
        return _CHIME_WAV_CACHE
    import io
    import math
    import struct
    import wave

    rate = 44100
    frames = max(1, int(rate * CHIME_DURATION_MS / 1000.0))
    fade = max(1, int(rate * 0.012))
    peak = int(CHIME_AMPLITUDE * 32767)
    pcm = bytearray()
    for i in range(frames):
        env = 1.0
        if i < fade:
            env = i / float(fade)
        elif i >= frames - fade:
            env = (frames - i) / float(fade)
        sample = int(peak * env * math.sin(2.0 * math.pi * CHIME_FREQ_HZ * i / rate))
        pcm += struct.pack("<h", sample)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes(pcm))
    _CHIME_WAV_CACHE = buffer.getvalue()
    return _CHIME_WAV_CACHE


def format_transcript_log(duration: float, infer_seconds: float, text: str | None,
                          log_transcript: bool) -> str:
    """识别完成日志的文案（隐私默认脱敏）。

    ``log_transcript=False``（默认）只记「文本长度=N 字」，不写识别内容；
    ``log_transcript=True`` 才记 ``文本=%r``（排错用，需用户显式改 config.json）。
    """
    base = "识别完成：%.2fs 音频 / %.2fs 推理" % (duration, infer_seconds)
    if log_transcript:
        return base + " / 文本=%r" % (text,)
    return base + " / 文本长度=%d 字" % len((text or "").strip())


def _setup_logging(cfg: dict, console: bool) -> None:
    level = getattr(logging, str(cfg.get("log_level", "info")).upper(), logging.INFO)
    cfgmod.LOG_DIR.mkdir(parents=True, exist_ok=True)
    logfile = cfgmod.LOG_DIR / "app.log"
    formatter = logging.Formatter("%(asctime)s %(levelname)-7s [%(threadName)s] %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    file_handler = logging.handlers.RotatingFileHandler(
        logfile, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    if console:
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(formatter)
        root.addHandler(stream)


def _single_instance_guard() -> bool:
    """返回 True 表示本进程是唯一实例；已存在实例时返回 False。"""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    handle = kernel32.CreateMutexW(None, True, "Local\\voice-ime-single-instance")
    err = ctypes.get_last_error()
    if not handle:
        LOG.warning("创建单实例互斥体失败，继续启动（err=%s）", err)
        return True
    if err == 183:  # ERROR_ALREADY_EXISTS
        return False
    globals()["_MUTEX_HANDLE"] = handle
    return True


def _signal_running_instance_quit() -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenEventW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.OpenEventW.restype = wintypes.HANDLE
    kernel32.SetEvent.argtypes = (wintypes.HANDLE,)
    kernel32.SetEvent.restype = wintypes.BOOL
    event = kernel32.OpenEventW(0x0002, False, "Local\\voice-ime-quit-event")
    if not event:
        return False
    try:
        return bool(kernel32.SetEvent(event))
    finally:
        kernel32.CloseHandle(event)


class _WinEventBase:
    """跨进程命名事件基类。"""

    NAME = ""

    def __init__(self, manual_reset: bool) -> None:
        import ctypes
        from ctypes import wintypes

        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel32.CreateEventW.argtypes = (
            wintypes.LPVOID,
            wintypes.BOOL,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        )
        self.kernel32.CreateEventW.restype = wintypes.HANDLE
        self.kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        self.kernel32.WaitForSingleObject.restype = wintypes.DWORD
        self.handle = self.kernel32.CreateEventW(None, manual_reset, False, self.NAME)

    def wait(self, timeout_ms: int) -> bool:
        if not self.handle:
            if timeout_ms > 0:
                time.sleep(timeout_ms / 1000.0)
            return False
        return self.kernel32.WaitForSingleObject(self.handle, timeout_ms) == 0


class _QuitEvent(_WinEventBase):
    NAME = "Local\\voice-ime-quit-event"

    def __init__(self) -> None:
        super().__init__(manual_reset=True)


class _ToggleEvent(_WinEventBase):
    NAME = "Local\\voice-ime-toggle-event"

    def __init__(self) -> None:
        super().__init__(manual_reset=False)


def _set_named_event(name: str) -> bool:
    """由 CLI 触发：向运行实例的命名事件发信号。"""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenEventW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.OpenEventW.restype = wintypes.HANDLE
    kernel32.SetEvent.argtypes = (wintypes.HANDLE,)
    kernel32.SetEvent.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    event = kernel32.OpenEventW(0x0002, False, name)
    if not event:
        return False
    try:
        return bool(kernel32.SetEvent(event))
    finally:
        kernel32.CloseHandle(event)


def _signal_running_instance_quit() -> bool:
    return _set_named_event("Local\\voice-ime-quit-event")


def _signal_running_instance_toggle() -> bool:
    return _set_named_event("Local\\voice-ime-toggle-event")


def _instance_running() -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenEventW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.OpenEventW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    event = kernel32.OpenEventW(0x0002, False, "Local\\voice-ime-quit-event")
    if not event:
        return False
    kernel32.CloseHandle(event)
    return True


class VoiceIME:
    """把 hotkey / audio / asr / inject / ui 串成输入法状态机。"""

    def __init__(self, cfg: dict, with_ui: bool = True) -> None:
        self.cfg = cfg
        self.with_ui = with_ui
        self.state = "idle"
        self._events: "queue.Queue[tuple]" = queue.Queue()
        self._lock = threading.RLock()
        self._recorder = None
        self._recognizer = None
        self._model_ready = threading.Event()
        self._model_error: str | None = None
        self._model_loading = False
        self._wav_path: str | None = None
        self._record_started_at = 0.0
        self._max_timer: threading.Timer | None = None
        self._ui = None
        self._hotkeys = None
        self._models: dict | None = None
        self._running = True
        self._shutdown_done = threading.Event()
        # 麦克风常驻采集（消除每次开麦 0.7~1.4s 延迟）
        self._capture = None
        self._capture_last_used = 0.0
        # 麦克风设备名缓存：成功解析后复用，空闲释放后重开麦不再重复 ffmpeg -list_devices
        # （实测枚举 129~204ms）；设备名失效时清空缓存并重新枚举重试。
        self._mic_device_cached: str | None = None
        # 冷启动计时起点：非 None 表示正在「打开麦克风」的过渡期（用于 ready 信号判定）
        self._cold_start_at: float | None = None
        # 用户按 toggle 撞上模型首次加载时置 True；模型加载完成后给“请再说一次”提示
        self._model_loaded_hint = False
        self._started_at = time.monotonic()
        self._record_mode: str | None = None  # warm | wav
        self._record_mark = 0
        self._pending_stop = False
        # 录音声纹：录音中把声压级推给悬浮球（~30fps 独立线程，不阻塞控制线程）
        self._level_pump: threading.Thread | None = None
        self._level_epoch = 0

    # ---------- 外部回调（热键线程 / UI 线程） ----------

    def post(self, name: str, *args) -> None:
        self._events.put((name,) + args)

    def _ui_state(self, state: str, message: str = "", bubble_ms: int | None = None,
                  log_message: str | None = None) -> None:
        """更新指示器状态；log_message 用于日志脱敏（None 时与气泡文案相同）。

        隐私：气泡可以显示识别文本预览，但日志不得输出识别文本，因此需要脱敏的
        调用点（如「已输入 <识别文本>」）通过 log_message 传安全文案。
        """
        LOG.info("state -> %s %s", state, message if log_message is None else log_message)
        if self._ui is not None:
            try:
                self._ui.set_state(state, message, bubble_ms)
            except Exception:  # UI 不能拖垮主流程
                LOG.exception("更新指示器失败")

    # ---------- 模型 ----------

    def _load_models(self) -> None:
        try:
            self._models = discover_models(self.cfg)
            LOG.info("模型：%s", self._models)
        except ModelMissingError as exc:
            self._model_error = str(exc)
            self._model_loading = False
            LOG.error("%s", exc)
            self.post("model_error", str(exc))
            return
        from voiceime.asr import SpeechRecognizer

        try:
            recognizer = SpeechRecognizer(
                model=self._models["model"],
                tokens=self._models["tokens"],
                vad_model=self._models.get("vad"),
                language=self.cfg.get("language", "auto"),
                use_itn=bool(self.cfg.get("use_itn", True)),
                num_threads=int(self.cfg.get("num_threads", 2)),
            )
            self._ui_state("loading", "加载模型中…")
            t0 = time.perf_counter()
            recognizer.load()
            LOG.info("模型加载完成：%.2fs", time.perf_counter() - t0)
            with self._lock:
                self._recognizer = recognizer
            self._model_ready.set()
            self.post("model_loaded")
        except Exception as exc:  # noqa: BLE001
            self._model_error = f"模型加载失败：{exc}"
            LOG.exception("模型加载失败")
            self.post("model_error", self._model_error)
        finally:
            self._model_loading = False

    def reload_model(self) -> None:
        self.post("reload_model")

    # ---------- 热键回调 ----------

    def on_toggle(self) -> None:
        self.post("toggle")

    def on_cancel(self) -> None:
        self.post("cancel")

    def on_quit(self) -> None:
        self.post("quit")

    # ---------- 录音 ----------

    def _ffmpeg(self) -> str:
        return cfgmod.ffmpeg_path(self.cfg)

    def _capture_enabled(self) -> bool:
        return bool(self.cfg.get("warm_mic", True))

    def _capture_warm(self) -> bool:
        stream = self._capture
        return stream is not None and stream.warm

    # ---------- 麦克风设备 / 常驻采集（含两项无损性能优化） ----------

    def _resolve_mic_device(self, force_refresh: bool = False) -> str:
        """解析麦克风设备名：优先复用缓存，force_refresh 时重新枚举。

        优化点：`resolve_device` 内部要跑一次 `ffmpeg -list_devices`（实测 129~204ms）。
        空闲释放麦克风后重开时复用上次成功解析的设备名，把这 129~204ms 省掉；
        设备名失效（拔插/改名）时由 `_ensure_warm_capture` 清缓存重试。
        """
        from voiceime.audio import resolve_device

        if not force_refresh:
            cached = self._mic_device_cached
            if cached:
                LOG.debug("复用缓存的麦克风设备：%s", cached)
                return cached
        device = resolve_device(self._ffmpeg(), self.cfg.get("mic_device"))
        self._mic_device_cached = device
        return device

    def _ensure_warm_capture(self, log_ready: bool = False):
        """确保常驻采集可用（必要时开麦）。设备名优先用缓存，失败后重新枚举重试一次。"""
        from voiceime.capture import MicrophoneStream

        with self._lock:
            stream = self._capture
            cached_hit = False
            if stream is None:
                cached_hit = bool(self._mic_device_cached)
                device = self._resolve_mic_device()
                stream = MicrophoneStream(
                    self._ffmpeg(),
                    device,
                    buffer_seconds=float(self.cfg.get("warm_mic_buffer_seconds", 180)),
                )
                self._capture = stream
        try:
            stream.start()
        except Exception:
            if not cached_hit:
                raise
            # 上次缓存的设备名可能已失效（USB 拔插/改名）：清缓存重新枚举，只重试一次
            LOG.warning("缓存的麦克风设备不可用，重新枚举设备后重试一次", exc_info=True)
            with self._lock:
                if self._capture is stream:
                    self._capture = None
            self._mic_device_cached = None
            device = self._resolve_mic_device(force_refresh=True)
            retry = MicrophoneStream(
                self._ffmpeg(),
                device,
                buffer_seconds=float(self.cfg.get("warm_mic_buffer_seconds", 180)),
            )
            with self._lock:
                self._capture = retry
            retry.start()
            stream = retry
        self._capture_last_used = time.monotonic()
        if log_ready:
            LOG.info("麦克风常驻采集就绪：%s", stream.device)
        return stream

    def _cold_elapsed_ms(self) -> float:
        """本次冷启动已耗时毫秒；没有进行中的冷启动返回 0。"""
        started = self._cold_start_at
        if started is None:
            return 0.0
        return (time.monotonic() - started) * 1000.0

    def _ready_chime_enabled(self) -> bool:
        ui_cfg = self.cfg.get("ui") if isinstance(self.cfg.get("ui"), dict) else {}
        return bool(ui_cfg.get("ready_chime", True))

    def _play_ready_chime(self) -> bool:
        """播放很轻的 ready 提示音（winsound + 内存 WAV，失败静默）。

        注意：CPython 的 winsound 不支持 `SND_MEMORY | SND_ASYNC` 组合（会抛
        RuntimeError），而同步播放 75ms 会阻塞控制线程；因此放到短命后台线程里
        用 `SND_MEMORY` 同步播放，既保持「内存生成」又不等它。
        """
        if sys.platform != "win32" or not self._ready_chime_enabled():
            return False
        try:
            import winsound

            data = _ready_chime_wav()

            def _play() -> None:
                try:
                    winsound.PlaySound(data, winsound.SND_MEMORY)
                except Exception:  # noqa: BLE001 - 提示音失败绝不影响录音
                    LOG.debug("ready 提示音播放失败（忽略）", exc_info=True)

            threading.Thread(target=_play, name="ready-chime", daemon=True).start()
            return True
        except Exception:  # noqa: BLE001 - 提示音失败绝不影响录音
            LOG.debug("ready 提示音初始化失败（忽略）", exc_info=True)
            return False

    def _start_capture_async(self) -> None:
        """后台预热常驻麦克风（失败不影响主流程，下次录音会回退按需采集）。"""
        if self._capture_warm():
            return
        threading.Thread(target=self._warm_capture_once, name="warm-mic", daemon=True).start()

    def _warm_capture_once(self) -> None:
        try:
            self._ensure_warm_capture(log_ready=True)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("麦克风常驻采集启动失败（下次录音回退按需采集）：%s", exc)
            with self._lock:
                self._capture = None

    def _ensure_model_loading(self) -> None:
        if self._recognizer is not None or self._model_loading:
            return
        self._model_loading = True
        threading.Thread(target=self._load_models, name="model", daemon=True).start()

    def _ensure_recognizer(self, timeout: float = 20.0):
        self._ensure_model_loading()
        if not self._model_ready.wait(timeout):
            raise RuntimeError(self._model_error or "模型加载超时，请稍后重试")
        recognizer = self._recognizer
        if recognizer is None:
            raise RuntimeError(self._model_error or "模型尚未就绪")
        return recognizer

    def _start_recording(self) -> None:
        """按下热键：优先用常驻采集（0 延迟），否则后台开麦/开流。

        冷启动（麦克风已被空闲释放/首次未就绪）时开麦要 ~0.6~0.9s：
        先弹「正在打开麦克风，请稍候…」气泡（loading 蓝色脉冲继续），避免用户以为卡住、
        直接开口丢字；真正就绪后再给「可以说话了」+ 轻提示音的就绪信号。
        暖启动（常驻采集还热着，toggle 即录）不弹过场、不响，保持安静。
        """
        if self._recognizer is None and not self._model_ready.is_set():
            if self._model_error:
                self._ui_state("error", "模型不可用")
                self._toast_later("error", f"模型不可用：{self._model_error[:30]}", 3000)
                return
            self._ensure_model_loading()
            self._model_loaded_hint = True
            # 首次加载模型也是冷启动：明确告诉用户“还没就绪、先别说话”
            self._ui_state("loading", "正在加载模型，请稍候…", bubble_ms=LOADING_BUBBLE_MS)
            return  # 模型约 1 秒就绪，用户再按一次即可
        self._pending_stop = False
        self.state = "preparing"
        if self._capture_enabled() and self._capture_warm():
            try:
                self._record_mark = self._capture.mark()
            except Exception as exc:  # noqa: BLE001
                LOG.warning("常驻采集切分失败，回退按需录音：%s", exc)
            else:
                self._record_mode = "warm"
                self._enter_recording()  # 暖启动：不弹过场、不响提示音
                return
        # 冷启动：先给「还没 ready」的明确过场，再去开麦
        self._cold_start_at = time.monotonic()
        self._ui_state("loading", "正在打开麦克风，请稍候…", bubble_ms=LOADING_BUBBLE_MS)
        threading.Thread(target=self._prepare_recording, name="prep-mic", daemon=True).start()

    def _prepare_recording(self) -> None:
        """后台打开常驻麦克风；失败则回退到按需 ffmpeg 录音。"""
        try:
            stream = self._ensure_warm_capture()
            mark = stream.mark()
            self.post("rec_started", mark)
            return
        except Exception as exc:  # noqa: BLE001
            LOG.warning("常驻采集启动失败，回退按需录音：%s", exc)
            with self._lock:
                self._capture = None
        self._prepare_wav_recording()

    def _prepare_wav_recording(self) -> None:
        from voiceime.audio import AudioRecorder

        last_error: Exception | None = None
        for attempt in (0, 1):
            try:
                device = self._resolve_mic_device(force_refresh=attempt > 0)
                recorder = AudioRecorder(self._ffmpeg(), device=device)
                recorder.start()
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                self._mic_device_cached = None
                if attempt == 0:
                    LOG.warning("按需录音启动失败，重新枚举设备后重试一次：%s", exc)
                    continue
            else:
                with self._lock:
                    self._recorder = recorder
                self.post("rec_started_wav")
                return
        LOG.error("录音启动失败：%s", last_error)
        self.post("rec_failed", str(last_error or "未知错误"))

    def _enter_recording(self, ready_signal: bool = False, cold_ms: float = 0.0) -> None:
        self._record_started_at = time.monotonic()
        self.state = "recording"
        if ready_signal:
            # 就绪双重信号：气泡「可以说话了」（~0.8s 后淡出）+ 很轻的提示音
            self._ui_state("recording", "可以说话了", bubble_ms=READY_BUBBLE_MS)
            played = self._play_ready_chime()
            LOG.info(
                "麦克风冷启动就绪：%.0f ms（气泡「可以说话了」+ ready 提示音=%s）",
                cold_ms, "已播放" if played else "关闭/不可用",
            )
        else:
            self._ui_state("recording", "请说话…")
        self._start_level_pump()
        limit = max(2, int(self.cfg.get("max_record_seconds", 60)))
        self._max_timer = threading.Timer(limit, lambda: self.post("auto_stop"))
        self._max_timer.daemon = True
        self._max_timer.start()

    # ---------- 录音声纹（推流给 UI） ----------

    def _start_level_pump(self) -> None:
        """录音中把声压级推给悬浮球（约 30fps）。

        常驻采集（warm）才有 level；按需 WAV 回退时告知 UI 不可用、保留麦克风动画。
        每个录音轮次用 epoch 区分，避免上一轮线程未退出导致新一轮没有推流。
        """
        ui = self._ui
        if ui is None:
            return
        available = self._record_mode == "warm" and self._capture is not None
        setter = getattr(ui, "set_level_available", None)
        if setter is not None:
            try:
                setter(available)
            except Exception:  # noqa: BLE001 - UI 推流失败不影响录音
                LOG.debug("设置声纹可用性失败", exc_info=True)
        if not available:
            return
        self._level_epoch += 1
        epoch = self._level_epoch
        self._level_pump = threading.Thread(
            target=self._level_pump_loop, args=(epoch,), name="level-pump", daemon=True
        )
        self._level_pump.start()

    def _level_pump_loop(self, epoch: int) -> None:
        """~30fps 读 capture.display_level 推给 UI；状态离开 recording 即退出。"""
        ui = self._ui
        interval = 1.0 / 30.0
        log_every = 30  # 每 30 帧（约 1s）记一行 level，作为测试/诊断证据
        counter = 0
        peak = 0.0
        try:
            while self.state == "recording" and self._running and epoch == self._level_epoch:
                stream = self._capture
                level = 0.0
                if stream is not None:
                    try:
                        level = float(stream.display_level)
                    except Exception:  # noqa: BLE001
                        level = 0.0
                if ui is not None:
                    try:
                        ui.set_level(level)
                    except Exception:  # noqa: BLE001
                        LOG.debug("推送声压级失败", exc_info=True)
                        break
                peak = max(peak, level)
                counter += 1
                if counter % log_every == 0:
                    LOG.info("录音声纹 level=%.3f peak=%.3f", level, peak)
                    peak = 0.0
                time.sleep(interval)
        finally:
            if self._level_pump is threading.current_thread():
                self._level_pump = None

    def _cancel_timer(self) -> None:
        if self._max_timer is not None:
            self._max_timer.cancel()
            self._max_timer = None

    def _stop_recording(self, cancelled: bool = False) -> None:
        self._cancel_timer()
        mode, self._record_mode = self._record_mode, None
        if mode == "warm":
            self._stop_warm_recording(cancelled)
        elif mode == "wav":
            self._stop_wav_recording(cancelled)
        else:
            self._to_idle()

    def _stop_warm_recording(self, cancelled: bool) -> None:
        stream = self._capture
        mark = self._record_mark
        if stream is None:
            self._ui_state("error", "录音失败")
            self._toast_later("error", "麦克风采集不可用", 2500)
            self._to_idle()
            return
        try:
            samples = stream.slice_since(mark)
        except Exception as exc:  # noqa: BLE001
            LOG.exception("音频切片失败")
            self._ui_state("error", "录音失败")
            self._toast_later("error", f"录音失败：{exc}"[:80], 3000)
            self._to_idle()
            return
        self._capture_last_used = time.monotonic()
        duration = len(samples) / 16000.0
        if cancelled:
            self._ui_state("idle", "已取消")
            self._to_idle()
            return
        if duration < float(self.cfg.get("min_record_seconds", 0.4)):
            self._ui_state("idle", "说话太短")
            self._to_idle()
            return
        self.state = "transcribing"
        self._ui_state("transcribing", "识别中…")
        threading.Thread(
            target=self._asr_worker, args=(samples, duration), name="asr", daemon=True
        ).start()

    def _stop_wav_recording(self, cancelled: bool) -> None:
        recorder = self._recorder
        with self._lock:
            self._recorder = None
        if recorder is None:
            self._to_idle()
            return
        duration = recorder.elapsed()
        try:
            wav_path = recorder.stop()
        except Exception as exc:  # noqa: BLE001
            LOG.exception("停止录音失败")
            self._ui_state("error", "录音失败")
            self._toast_later("error", f"录音失败：{exc}"[:80], 3000)
            try:
                recorder.cancel()
            except Exception:  # noqa: BLE001
                pass
            self._to_idle()
            return
        if cancelled:
            self._delete_quietly(wav_path)
            self._ui_state("idle", "已取消")
            self._to_idle()
            return
        if duration < float(self.cfg.get("min_record_seconds", 0.4)):
            self._delete_quietly(wav_path)
            self._ui_state("idle", "说话太短")
            self._to_idle()
            return
        self.state = "transcribing"
        self._ui_state("transcribing", "识别中…")
        threading.Thread(
            target=self._asr_worker, args=(wav_path, duration), name="asr", daemon=True
        ).start()

    def _asr_worker(self, payload, duration: float) -> None:
        """payload：np.ndarray（常驻采集切片）或 WAV 绝对路径（按需录音兜底）。"""
        try:
            recognizer = self._ensure_recognizer()
            if isinstance(payload, str):
                transcript = recognizer.transcribe_wav(payload)
            else:
                transcript = recognizer.transcribe_samples(payload, 16000)
            self.post("asr_done", transcript.text, duration, transcript.infer_seconds)
        except Exception as exc:  # noqa: BLE001
            LOG.exception("识别失败")
            self.post("asr_error", str(exc))
        finally:
            if isinstance(payload, str):
                self._delete_quietly(payload)

    # ---------- 文本注入 ----------

    def _inject(self, text: str) -> None:
        from voiceime.inject import insert_text

        insert_text(
            text,
            mode=str(self.cfg.get("insert_mode", "paste")),
            restore_clipboard=bool(self.cfg.get("restore_clipboard", True)),
            paste_delay_ms=int(self.cfg.get("paste_delay_ms", 60)),
            restore_delay_ms=int(self.cfg.get("restore_delay_ms", 350)),
        )

    # ---------- 状态 ----------

    def _to_idle(self) -> None:
        self.state = "idle"
        if self._ui is not None:
            try:
                self._ui.set_state("idle")
            except Exception:  # noqa: BLE001
                LOG.exception("恢复 idle 失败")

    def _toast_later(self, state: str, message: str, delay_ms: int) -> None:
        def _reset() -> None:
            time.sleep(delay_ms / 1000.0)
            # 过期 toast 只允许把「空闲/出错」拉回 idle：若用户在这 2.2s 内已经开了新一轮
            # 录音/识别，旧定时器绝不能把它们重置回 idle（实测快速点击会踩到，
            # 表现是“录音中突然回到 idle、下一次点击无法停止录音”）。
            if self.state in ("recording", "preparing", "transcribing"):
                LOG.debug("过期提示定时器忽略（当前状态 %s）", self.state)
                return
            self._to_idle()

        threading.Thread(target=_reset, name="toast-reset", daemon=True).start()

    @staticmethod
    def _delete_quietly(path: str | None) -> None:
        if not path:
            return
        try:
            os.unlink(path)
        except OSError:
            pass

    # ---------- 控制线程 ----------

    def run(self) -> None:
        threading.Thread(target=self._control_loop, name="control", daemon=True).start()
        self._ensure_model_loading()
        if self._capture_enabled():
            self._start_capture_async()
        threading.Thread(target=self._capture_reaper_loop, name="mic-reaper", daemon=True).start()
        self._hotkeys = self._make_hotkeys()
        if self._hotkeys is not None:
            try:
                self._hotkeys.start()
            except Exception as exc:  # noqa: BLE001
                LOG.exception("全局热键注册失败（可能被安全软件拦截）")
                self._ui_state("error", "热键不可用")
                self._toast_later("error", f"热键不可用：{exc}"[:80], 5000)

    def _capture_reaper_loop(self) -> None:
        """空闲超时后释放麦克风（避免常驻占用影响其它通话/录音程序）。"""
        while self._running:
            time.sleep(5.0)
            if not self._capture_enabled() or self.state != "idle":
                continue
            stream = self._capture
            if stream is None or not stream.warm:
                continue
            idle_seconds = float(self.cfg.get("warm_mic_idle_seconds", 120))
            last_used = max(self._capture_last_used, self._started_at)
            if idle_seconds > 0 and time.monotonic() - last_used > idle_seconds:
                LOG.info("空闲超过 %.0fs，释放常驻麦克风", idle_seconds)
                try:
                    stream.close()
                except Exception:  # noqa: BLE001
                    LOG.exception("关闭常驻麦克风失败")
                with self._lock:
                    self._capture = None

    def _make_hotkeys(self):
        from voiceime.hotkey import HotkeyManager

        try:
            return HotkeyManager(
                hotkey=str(self.cfg.get("hotkey", "ctrl+alt+space")),
                cancel_key=str(self.cfg.get("cancel_key", "esc")),
                quit_hotkey=self.cfg.get("quit_hotkey"),
                on_toggle=self.on_toggle,
                on_cancel=self.on_cancel,
                on_quit=self.on_quit,
            )
        except Exception:  # noqa: BLE001
            LOG.exception("热键初始化失败")
            self._ui_state("error", "热键不可用")
            return None

    def _control_loop(self) -> None:
        quitter = _QuitEvent()
        toggler = _ToggleEvent()
        while self._running:
            if quitter.wait(0):
                try:
                    self._handle_event(("quit",))
                except Exception:  # noqa: BLE001
                    LOG.exception("退出处理异常")
                break
            if toggler.wait(0):
                self.post("toggle")
                continue
            try:
                event = self._events.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                self._handle_event(event)
            except Exception:  # noqa: BLE001
                LOG.exception("事件处理异常：%s", event[:1])
        self._shutdown_done.set()

    def _handle_event(self, event: tuple) -> None:
        name = event[0]
        if name == "toggle":
            if self.state == "idle":
                self._start_recording()
            elif self.state == "recording":
                self._stop_recording()
            elif self.state == "preparing":
                self._pending_stop = True
                LOG.info("麦克风仍在校验/打开中，已记录取消请求")
            else:
                LOG.info("识别中，忽略切换热键")
        elif name == "cancel":
            if self.state == "recording":
                self._stop_recording(cancelled=True)
            elif self.state == "preparing":
                self._pending_stop = True
        elif name == "rec_started":
            mark = int(event[1])
            cold_ms = self._cold_elapsed_ms()
            self._cold_start_at = None
            if self._pending_stop:
                self._pending_stop = False
                self.state = "idle"
                self._ui_state("idle", "已取消")
                return
            self._record_mode = "warm"
            self._record_mark = mark
            self._enter_recording(
                ready_signal=_should_signal_ready(cold_ms), cold_ms=cold_ms
            )
        elif name == "rec_started_wav":
            cold_ms = self._cold_elapsed_ms()
            self._cold_start_at = None
            if self._pending_stop:
                self._pending_stop = False
                self._stop_wav_recording(cancelled=True)
                return
            self._record_mode = "wav"
            self._enter_recording(
                ready_signal=_should_signal_ready(cold_ms), cold_ms=cold_ms
            )
        elif name == "rec_failed":
            self._cold_start_at = None
            self._pending_stop = False
            self.state = "idle"
            # 失败必须响亮：明确告诉你「麦克风打开失败」，不要静默回到 idle
            self._ui_state("error", "麦克风打开失败")
            self._toast_later("error", f"麦克风打开失败：{event[1]}"[:80], 3500)
        elif name == "auto_stop":
            if self.state == "recording":
                LOG.info("达到最长录音时长，自动结束")
                self._stop_recording()
        elif name == "asr_done":
            text, duration, infer_seconds = event[1], event[2], event[3]
            # 隐私：默认只记文本长度，不把识别内容写进日志（log_transcript=True 才记原文）
            LOG.info(
                "%s",
                format_transcript_log(
                    duration, infer_seconds, text,
                    bool(self.cfg.get("log_transcript", False)),
                ),
            )
            text = (text or "").strip()
            if not text:
                self._ui_state("error", "没听清")
                self._toast_later("error", "没听清，请再说一次", 2200)
                self._to_idle()
                return
            try:
                self._inject(text)
            except Exception as exc:  # noqa: BLE001
                LOG.exception("文本注入失败")
                self._ui_state("error", "输入失败")
                self._toast_later("error", f"输入失败：{exc}"[:80], 3000)
                self._to_idle()
                return
            preview = text if len(text) <= 12 else text[:12] + "…"
            # 气泡给文本预览，日志只记状态 + 脱敏文案（不得输出识别文本）
            self._ui_state(
                "idle", "已输入 " + preview, log_message="已输入（识别文本不写日志）"
            )
            self._to_idle()
        elif name == "asr_error":
            self._ui_state("error", "识别失败")
            self._toast_later("error", f"识别失败：{event[1]}"[:80], 3000)
            self._to_idle()
        elif name == "model_loaded":
            if self.state == "idle" and self._ui is not None:
                if self._model_loaded_hint:
                    # 用户刚按过 toggle 撞上首次加载模型：明确告知现在可以说了
                    self._model_loaded_hint = False
                    self._ui_state("idle", "模型已就绪，请再说一次")
                else:
                    self._ui.set_state("idle", "就绪")
        elif name == "model_error":
            self._ui_state("error", "模型不可用")
            self._toast_later("error", str(event[1])[:80], 4000)
        elif name == "reload_model":
            self._do_reload_model()
        elif name == "quit":
            self._running = False
            self.shutdown()

    def _do_reload_model(self) -> None:
        LOG.info("重新加载模型")
        with self._lock:
            self._recognizer = None
        self._model_ready.clear()
        self._model_error = None
        self._model_loading = True
        threading.Thread(target=self._load_models, name="model", daemon=True).start()

    def shutdown(self) -> None:
        LOG.info("退出中…")
        if self._recorder is not None:
            try:
                self._recorder.cancel()
            except Exception:  # noqa: BLE001
                pass
            self._recorder = None
        capture = self._capture
        if capture is not None:
            try:
                capture.close()
            except Exception:  # noqa: BLE001
                LOG.exception("关闭常驻麦克风失败")
            self._capture = None
        if self._hotkeys is not None:
            try:
                self._hotkeys.stop()
            except Exception:  # noqa: BLE001
                LOG.exception("停止热键监听失败")
        if self._ui is not None:
            try:
                self._ui.quit()
            except Exception:  # noqa: BLE001
                LOG.exception("关闭界面失败")
        try:
            cfgmod.save_config(self.cfg)
        except Exception:  # noqa: BLE001
            LOG.exception("保存配置失败")
        self._shutdown_done.set()

    def set_ui(self, ui) -> None:
        self._ui = ui

    def wait_shutdown(self, timeout: float = 5.0) -> bool:
        return self._shutdown_done.wait(timeout)


# ---------- CLI ----------


def _print_check(cfg: dict) -> int:
    from voiceime.paths import discover_models

    print("== voice-ime 自检 ==")
    ok = True
    try:
        models = discover_models(cfg)
        print(f"[OK] 模型 model : {models['model']}")
        print(f"[OK] 词表 tokens: {models['tokens']}")
        print(f"[OK] VAD       : {models['vad'] or '(未找到，长音频将整段解码)'}")
    except ModelMissingError as exc:
        ok = False
        print(f"[FAIL] 模型: {exc}")
    try:
        ffmpeg = cfgmod.ffmpeg_path(cfg)
        print(f"[OK] ffmpeg    : {ffmpeg}")
    except cfgmod.ConfigError as exc:
        ok = False
        ffmpeg = None
        print(f"[FAIL] ffmpeg: {exc}")
    if ffmpeg:
        try:
            from voiceime.audio import list_input_devices, resolve_device

            devices = list_input_devices(ffmpeg)
            print(f"[OK] 麦克风    : {len(devices)} 个 -> {devices}")
            print(f"[OK] 默认设备  : {resolve_device(ffmpeg, cfg.get('mic_device'))}")
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"[FAIL] 麦克风: {exc}")
    from voiceime.inject import get_clipboard_text

    try:
        get_clipboard_text()
        print("[OK] 剪贴板    : 可读")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"[FAIL] 剪贴板: {exc}")
    print("== 结果：" + ("全部可用" if ok else "存在不可用项") + " ==")
    return 0 if ok else 1


def _transcribe_file(cfg: dict, wav_path: str, insert: bool, language: str | None) -> int:
    from voiceime.asr import SpeechRecognizer

    models = discover_models(cfg)
    recognizer = SpeechRecognizer(
        model=models["model"],
        tokens=models["tokens"],
        vad_model=models.get("vad"),
        language=language or cfg.get("language", "auto"),
        use_itn=bool(cfg.get("use_itn", True)),
        num_threads=int(cfg.get("num_threads", 2)),
    )
    recognizer.load()
    transcript = recognizer.transcribe_wav(wav_path, language=language)
    print(f"audio={transcript.audio_seconds:.2f}s infer={transcript.infer_seconds:.2f}s "
          f"rtf={transcript.infer_seconds / max(transcript.audio_seconds, 1e-9):.3f}")
    print("TEXT=" + transcript.text)
    if insert and transcript.text.strip():
        from voiceime.inject import insert_text

        insert_text(
            transcript.text.strip(),
            mode=str(cfg.get("insert_mode", "paste")),
            restore_clipboard=bool(cfg.get("restore_clipboard", True)),
            paste_delay_ms=int(cfg.get("paste_delay_ms", 60)),
            restore_delay_ms=int(cfg.get("restore_delay_ms", 350)),
        )
        print("[OK] 已注入焦点窗口")
    return 0


def _enable_dpi_awareness() -> None:
    """让 Tk 按物理像素渲染（否则 125%/150% 缩放下玻璃球会被系统拉伸发虚）。"""
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # PROCESS_SYSTEM_DPI_AWARE
        except Exception:  # noqa: BLE001 - 旧系统回退
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001 - 非 Windows/受限环境忽略
        pass


def _use_utf8_stdio() -> None:
    """CLI 输出统一 UTF-8（Windows 控制台默认 GBK，会让中文在管道里乱码）。"""
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


class _NullTextIO:
    """无控制台（pythonw / PyInstaller --noconsole）时的输出黑洞。"""

    encoding = "utf-8"

    def write(self, text: str) -> int:
        return len(text)

    def flush(self) -> None:
        return None


def _attach_console_if_needed() -> bool:
    """返回是否已有可用控制台；EXE 从终端带参数运行时附加父控制台（否则输出黑洞）。"""
    if sys.stdout is not None and hasattr(sys.stdout, "write"):
        _use_utf8_stdio()
        return True
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if kernel32.AttachConsole(0xFFFFFFFF):  # ATTACH_PARENT_PROCESS
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
            sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
            return True
    except Exception:  # noqa: BLE001
        pass
    sys.stdout = _NullTextIO()  # type: ignore[assignment]
    sys.stderr = _NullTextIO()  # type: ignore[assignment]
    return False


def _show_text(text: str, title: str = "voice-ime") -> None:
    """无控制台时用消息框展示命令行结果（EXE 双击 --check 等场景）。"""
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, title, 0x00000040)
    except Exception:  # noqa: BLE001
        pass


def _run_captured(func, *args, title: str) -> int:
    """无控制台时把打印内容收集起来用消息框展示。"""
    import contextlib
    import io

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = func(*args)
    _show_text(buffer.getvalue(), title)
    return code


def main(argv: list[str] | None = None) -> int:
    _enable_dpi_awareness()
    console = _attach_console_if_needed()
    parser = argparse.ArgumentParser(description="voice-ime：Windows 本地语音输入法")
    parser.add_argument("--check", action="store_true", help="环境自检")
    parser.add_argument("--file", metavar="WAV", help="离线识别 WAV 文件")
    parser.add_argument("--insert", action="store_true", help="配合 --file：识别后注入焦点窗口")
    parser.add_argument("--language", help="语言：auto/zh/en/yue/ja/ko")
    parser.add_argument("--quit", action="store_true", help="通知已运行实例退出")
    parser.add_argument("--toggle", action="store_true", help="通知已运行实例开始/停止录音（调试/自动化）")
    parser.add_argument("--status", action="store_true", help="查看是否已有实例在运行")
    parser.add_argument("--no-ui", action="store_true", help="不显示悬浮球")
    parser.add_argument("--console", action="store_true", help="日志同时输出到控制台")
    args = parser.parse_args(argv)

    if args.status:
        running = _instance_running()
        print("voice-ime 正在运行" if running else "voice-ime 未运行")
        return 0 if running else 1
    if args.toggle:
        done = _signal_running_instance_toggle()
        print("已通知运行实例切换录音" if done else "没有检测到运行中的 voice-ime")
        return 0 if done else 1
    if args.quit:
        done = _signal_running_instance_quit()
        print("已通知运行中的 voice-ime 退出" if done else "没有检测到运行中的 voice-ime")
        return 0 if done else 1

    try:
        cfg = cfgmod.load_config()
    except cfgmod.ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    _setup_logging(cfg, console=args.console or not (args.check or args.file))
    LOG.info(
        "隐私：识别文字写日志=%s（false 时日志只记文本长度，不记内容）",
        bool(cfg.get("log_transcript", False)),
    )

    if args.check:
        if console:
            return _print_check(cfg)
        return _run_captured(_print_check, cfg, title="voice-ime 自检")
    if args.file:
        try:
            if console:
                return _transcribe_file(cfg, args.file, args.insert, args.language)
            return _run_captured(
                _transcribe_file, cfg, args.file, args.insert, args.language,
                title="voice-ime 识别结果",
            )
        except Exception as exc:  # noqa: BLE001
            print(f"识别失败：{exc}", file=sys.stderr)
            LOG.exception("离线识别失败")
            return 1

    if not _single_instance_guard():
        print("voice-ime 已在运行；如需重启请先 `python app.py --quit`", file=sys.stderr)
        return 3

    app = VoiceIME(cfg, with_ui=not args.no_ui)
    ui = None
    if not args.no_ui:
        from voiceime.ui import Indicator

        ui = Indicator(
            cfg,
            on_toggle=app.on_toggle,
            on_quit=app.on_quit,
            on_reload_model=app.reload_model,
        )
        app.set_ui(ui)
    app.run()
    LOG.info("voice-ime 已启动（热键 %s）", cfg.get("hotkey"))
    if ui is not None:
        try:
            ui.run()
        except KeyboardInterrupt:
            app.post("quit")
    else:
        try:
            while app._running:
                time.sleep(0.2)
        except KeyboardInterrupt:
            app.post("quit")
    app.wait_shutdown(5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
