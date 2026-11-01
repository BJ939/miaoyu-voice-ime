# -*- coding: utf-8 -*-
"""常驻麦克风采集：ffmpeg dshow → stdout PCM 环形缓冲。

为什么要常驻：每次按下热键才启动 ffmpeg 打开麦克风需要 0.7~1.4 秒，
用户看到“录音中”之前说的话会丢。常驻采集后，按下热键只是记录一个字节偏移，
松开是一个内存切片，延迟 ~0ms，且不截断第一个字。

隐私与兼容：麦克风常驻只在最近使用后的空闲期内保持（见 config.warm_mic_idle_seconds），
空闲超时会自动关闭释放设备；其它程序独占麦克风时采集进程会退出，app 会退回按需录音。
"""
from __future__ import annotations

import subprocess
import threading
import time
from collections import deque

import numpy as np

from .audio_level import LevelMeter

# 新建进程不弹出控制台窗口（pythonw/PyInstaller --windowed 运行时必须）：
# EXE 无控制台父进程，spawn ffmpeg 这类控制台程序时 Windows 会新建控制台窗口，
# 用户会看到「空闲释放麦克风后重新开麦时弹出一个 CMD/控制台窗口」。
# 与 voiceime/audio.py 的 _CREATE_NO_WINDOW 保持一致（同名常量、同一取值来源）。
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 等待首批音频的时长（秒）。原 200ms → 120ms（2026-10-31 冷启动延迟优化）：
# 实测 ffmpeg 打开设备后首字节约 645ms 到达，再等满 120ms 已足够确认数据流稳定；
# 缩短让「空闲释放后重新开麦」的冷启动少等 ~80ms，不丢音/不爆音（真机验证见报告）。
FIRST_AUDIO_WAIT_SECONDS = 0.12


class CaptureError(Exception):
    """麦克风常驻采集失败（消息为可读中文）。"""


class MicrophoneStream:
    """ffmpeg 持续采集 + 内存环形缓冲，按绝对字节偏移切片。"""

    def __init__(
        self,
        ffmpeg_path: str,
        device: str,
        sample_rate: int = 16000,
        buffer_seconds: float = 180.0,
        audio_buffer_ms: int = 100,
    ) -> None:
        self.ffmpeg_path = ffmpeg_path
        self.device = device
        self.sample_rate = sample_rate
        self._max_bytes = int(max(10.0, buffer_seconds) * sample_rate * 2)
        self._audio_buffer_ms = audio_buffer_ms
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._stderr: deque[str] = deque(maxlen=20)
        self._cond = threading.Condition()
        self._buffer = bytearray()
        self._base = 0  # _buffer[0] 对应的绝对字节偏移
        self._total = 0  # 已采集的绝对字节数
        self._alive = False
        self._error: str | None = None
        self._start_lock = threading.Lock()
        # 录音声压级：读线程每个 chunk 喂一次，UI 线程只读 level/raw_level。
        self.meter = LevelMeter(sample_rate=sample_rate)

    # ---------- 状态 ----------

    @property
    def warm(self) -> bool:
        """采集进程存活且已收到音频数据。"""
        with self._cond:
            return bool(self._alive and self._total > 0)

    @property
    def error(self) -> str | None:
        with self._cond:
            return self._error

    @property
    def level(self) -> float:
        """当前录音声压级 0..1（平滑包络，供录音态声纹使用）。"""
        return self.meter.level

    @property
    def raw_level(self) -> float:
        """当前声压级 0..1（未平滑，诊断用）。"""
        return self.meter.raw_level

    @property
    def display_level(self) -> float:
        """当前声压级 0..1（自适应噪声门版本，静音为 0，UI 声纹用）。"""
        return self.meter.display_level

    # ---------- 生命周期 ----------

    def start(self, timeout: float = 10.0) -> None:
        """启动采集并等到首批音频到达（并发调用会串行等待，避免重复开流）。"""
        with self._start_lock:
            self._start_locked(timeout)

    def _start_locked(self, timeout: float) -> None:
        if self.warm:
            return
        self.close()
        args = [
            self.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-f",
            "dshow",
            "-audio_buffer_size",
            str(self._audio_buffer_ms),
            "-i",
            f"audio={self.device}",
            "-ac",
            "1",
            "-ar",
            str(self.sample_rate),
            "-f",
            "s16le",
            "-",
        ]
        try:
            proc = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                creationflags=_CREATE_NO_WINDOW,
            )
        except OSError as exc:
            raise CaptureError(f"无法启动 ffmpeg 采集进程：{exc}") from exc
        with self._cond:
            self._proc = proc
            self._alive = True
            self._error = None
            self._buffer = bytearray()
            self._base = 0
            self._total = 0
            self._stderr.clear()
        self.meter.reset()
        self._reader = threading.Thread(target=self._read_loop, args=(proc,), name="mic-reader", daemon=True)
        self._reader.start()
        threading.Thread(target=self._stderr_loop, args=(proc,), name="mic-stderr", daemon=True).start()

        # 等到收到至少 FIRST_AUDIO_WAIT_SECONDS 音频，确保设备真正打开且数据流稳定
        # （原 200ms；缩短到 120ms 是冷启动延迟优化，实测数据流稳定、不丢音）。
        need = int(self.sample_rate * 2 * FIRST_AUDIO_WAIT_SECONDS)
        deadline = time.monotonic() + max(1.0, timeout)
        with self._cond:
            while time.monotonic() < deadline:
                if self._total >= need:
                    return
                if not self._alive:
                    break
                self._cond.wait(0.05)
            detail = self.error or "、".join(list(self._stderr)[-2:]) or "未收到音频数据"
        self.close()
        raise CaptureError(f"打开麦克风失败：{detail}")

    def close(self) -> None:
        proc, self._proc = self._proc, None
        with self._cond:
            self._alive = False
            self._cond.notify_all()
        self.meter.reset()
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
        except OSError:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception:  # noqa: BLE001
                pass
        for pipe in (proc.stdout, proc.stderr):
            try:
                if pipe is not None:
                    pipe.close()
            except Exception:  # noqa: BLE001
                pass
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.join(timeout=1.0)

    # ---------- 切片 ----------

    def mark(self) -> int:
        """记录当前采集位置（绝对字节偏移）。"""
        with self._cond:
            return self._total

    def available_seconds(self) -> float:
        with self._cond:
            return (self._total - self._base) / 2.0 / self.sample_rate

    def slice_since(self, mark: int, flush_ms: int = 200) -> np.ndarray:
        """取 mark 之后的音频（float32，[-1,1]）。flush_ms 用来等管道尾部数据到达。"""
        if flush_ms > 0:
            time.sleep(flush_ms / 1000.0)
        with self._cond:
            start = max(int(mark), self._base)
            offset = start - self._base
            if offset >= len(self._buffer):
                return np.zeros(0, dtype=np.float32)
            data = bytes(self._buffer[offset:])
        if len(data) % 2:
            data = data[:-1]
        if not data:
            return np.zeros(0, dtype=np.float32)
        pcm = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        return pcm

    # ---------- 内部线程 ----------

    def _read_loop(self, proc: subprocess.Popen) -> None:
        stream = proc.stdout
        assert stream is not None
        while True:
            try:
                chunk = stream.read(4096)
            except (OSError, ValueError):
                chunk = b""
            if not chunk:
                break
            with self._cond:
                self._buffer.extend(chunk)
                self._total += len(chunk)
                overflow = len(self._buffer) - self._max_bytes
                if overflow > 0:
                    if overflow % 2:
                        overflow += 1
                    del self._buffer[:overflow]
                    self._base += overflow
                self._cond.notify_all()
            # 声压级：拿完缓冲锁再喂，避免 UI 读 level 时被缓冲锁串住
            self.meter.feed(chunk)
        with self._cond:
            self._alive = False
            if not self._error:
                tail = "、".join(list(self._stderr)[-2:])
                self._error = f"麦克风采集进程已退出{('：' + tail) if tail else ''}"
            self._cond.notify_all()

    def _stderr_loop(self, proc: subprocess.Popen) -> None:
        stream = proc.stderr
        if stream is None:
            return
        while True:
            try:
                line = stream.readline()
            except (OSError, ValueError):
                return
            if not line:
                return
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                with self._cond:
                    self._stderr.append(text)


def transcribe_pcm(samples: np.ndarray, sample_rate: int = 16000) -> float:
    """返回音频秒数（供 app 做最短时长判断）。"""
    return len(samples) / float(sample_rate)
