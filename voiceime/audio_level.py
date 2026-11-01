# -*- coding: utf-8 -*-
"""录音声压级（LevelMeter）：把 s16le 原始 PCM 变成 0..1 的平滑包络。

用途：录音中把麦克风图标换成实时声纹——有声音就波动，没声音就是一条平线。
设计要求（实测口径见 tests/test_waveform.py）：

- 输入：16k / mono / s16le 原始字节，任意长度；内部按 10ms 小帧对齐，剩余字节留到下次。
- 输出：``level`` 为 0..1 的平滑包络（attack≈15ms、release≈180ms），
  由每帧 RMS 转 dBFS 后线性映射：-60dBFS→0、-6dBFS→1（超出裁剪）。
- ``display_level``：给 UI 用的「自适应噪声门」版本。实测本机麦克风常驻底噪约 -32dBFS，
  固定映射下静音会显示 level≈0.3（波形一直在抖），与用户要求的「没声音就是平线」冲突。
  因此另算一路：跟踪噪声底（快降慢升），门限=底噪+12dB，门限～-6dBFS 线性映射 0..1，
  再减去底部 12% 死区（压掉门限附近的底噪抖动），静音时稳定 0（平线），
  说话时 0.5~1.0（明显波动）。``level`` 仍保留规格口径供测试/诊断。
- ``raw_level``：最后一帧未平滑的映射值（给诊断/测试看上升是否跟手）。
- ``peak``：带衰减的峰值保持（默认 2.0/s 线性衰减），供 UI 做峰值条可选。
- 线程安全：``feed`` 由采集读线程调用，``level`` 等属性由 UI 线程读取，内部 Lock 保护。

计算成本：一个 4096 字节 chunk（128ms 音频）内部 12.8 帧，numpy 向量化 + 标量平滑，
实测约 20~60µs / chunk，对常驻采集读线程无感。
"""
from __future__ import annotations

import math
import threading
from collections import deque

import numpy as np

__all__ = ["LevelMeter", "dbfs_to_level", "level_from_rms"]

# 10ms 内部帧：既能算出正确的 RMS，又让 attack/release 时间常数与 chunk 大小无关
INTERNAL_HOP_MS = 10.0


def level_from_rms(rms: float, floor_db: float = -60.0, ceil_db: float = -6.0) -> float:
    """RMS（0..1 浮点幅度）→ dBFS → 0..1 的线性映射值（未平滑）。"""
    if rms <= 0.0:
        return 0.0
    return dbfs_to_level(20.0 * math.log10(rms), floor_db=floor_db, ceil_db=ceil_db)


def dbfs_to_level(dbfs: float, floor_db: float = -60.0, ceil_db: float = -6.0) -> float:
    """dBFS → 0..1（-floor 以下为 0，ceil 以上为 1，中间线性）。"""
    span = ceil_db - floor_db
    if span <= 0:
        return 0.0
    if dbfs == float("-inf"):
        return 0.0
    value = (dbfs - floor_db) / span
    if value <= 0.0:
        return 0.0
    if value >= 1.0:
        return 1.0
    return float(value)


class LevelMeter:
    """线程安全的录音声压级计。

    >>> meter = LevelMeter(16000)
    >>> meter.feed(b"\\x00\\x00" * 1600)   # 0.1s 静音
    >>> meter.level
    0.0
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        attack_ms: float = 15.0,
        release_ms: float = 180.0,
        floor_db: float = -60.0,
        ceil_db: float = -6.0,
        peak_decay_per_second: float = 2.0,
        history_seconds: float = 2.0,
        gate_margin_db: float = 12.0,
        display_deadzone: float = 0.12,
        noise_floor_rise_db_per_second: float = 1.5,
        noise_floor_fall_coeff: float = 0.15,
        noise_floor_min_db: float = -85.0,
        noise_floor_max_db: float = -20.0,
        min_gate_span_db: float = 12.0,
    ) -> None:
        if sample_rate <= 0:
            raise ValueError("sample_rate 必须为正数")
        self.sample_rate = int(sample_rate)
        self.attack_ms = float(attack_ms)
        self.release_ms = float(release_ms)
        self.floor_db = float(floor_db)
        self.ceil_db = float(ceil_db)
        self.peak_decay_per_second = float(peak_decay_per_second)
        self.gate_margin_db = float(gate_margin_db)
        self.display_deadzone = float(display_deadzone)
        self.noise_floor_rise = float(noise_floor_rise_db_per_second)
        self.noise_floor_fall_coeff = float(noise_floor_fall_coeff)
        self.noise_floor_min_db = float(noise_floor_min_db)
        self.noise_floor_max_db = float(noise_floor_max_db)
        self.min_gate_span_db = float(min_gate_span_db)

        self._hop = max(1, int(round(self.sample_rate * INTERNAL_HOP_MS / 1000.0)))
        self._hop_seconds = self._hop / float(self.sample_rate)
        self._frame_bytes = self._hop * 2
        # 一阶平滑系数：每 10ms 帧按时间常数换算，chunk 大小不影响手感
        self._attack_coeff = self._smooth_coeff(self.attack_ms)
        self._release_coeff = self._smooth_coeff(self.release_ms)

        self._lock = threading.Lock()
        self._pending = b""  # 不足一帧的尾部字节
        self._level = 0.0
        self._display = 0.0  # 自适应噪声门后的显示值（UI 用）
        self._raw = 0.0
        self._peak = 0.0
        self._rms = 0.0
        self._dbfs = float("-inf")
        self._noise_floor_db: float | None = None  # 自适应底噪（首次喂音频时初始化）
        self._gate_db: float | None = None
        self._frames_total = 0
        history_frames = max(8, int(round(history_seconds / self._hop_seconds)))
        self._history: deque[float] = deque(maxlen=history_frames)

    # ---------- 平滑 ----------

    def _smooth_coeff(self, tau_ms: float) -> float:
        """返回 1 - exp(-dt/tau)；dt 固定为内部帧长。"""
        tau = max(1e-6, tau_ms / 1000.0)
        return 1.0 - math.exp(-self._hop_seconds / tau)

    # ---------- 写入（采集读线程） ----------

    def feed(self, pcm_bytes: bytes) -> None:
        """吃一段 s16le 原始字节（任意长度，内部帧对齐）。"""
        if not pcm_bytes:
            return
        with self._lock:
            data = self._pending + pcm_bytes
            usable = (len(data) // self._frame_bytes) * self._frame_bytes
            if usable <= 0:
                self._pending = data
                return
            block = data[:usable]
            self._pending = data[usable:]
            self._apply_block(block)

    def _apply_block(self, block: bytes) -> None:
        # 调用方已持锁
        samples = np.frombuffer(block, dtype="<i2").astype(np.float32) * (1.0 / 32768.0)
        frames = samples.reshape(-1, self._hop)
        rms = np.sqrt(np.mean(frames * frames, axis=1))
        with np.errstate(divide="ignore"):
            dbfs = np.where(rms > 0.0, 20.0 * np.log10(np.maximum(rms, 1e-12)), -120.0)
        span = self.ceil_db - self.floor_db
        mapped = np.clip((dbfs - self.floor_db) / span, 0.0, 1.0)
        mapped_list = mapped.tolist()
        dbfs_list = dbfs.tolist()

        level = self._level
        display = self._display
        peak = self._peak
        floor = self._noise_floor_db
        gate_db = self._gate_db
        dt = self._hop_seconds
        peak_fall = self.peak_decay_per_second * dt
        floor_rise = self.noise_floor_rise * dt
        for i, target in enumerate(mapped_list):
            # 1) 规格口径的平滑包络（固定 -60dB→0 / -6dB→1）
            coeff = self._attack_coeff if target > level else self._release_coeff
            level += (target - level) * coeff

            # 2) 自适应噪声底：快降慢升；门限 = 底噪 + margin，再映射到 0..1
            frame_db = dbfs_list[i]
            if floor is None:
                floor = frame_db
            elif frame_db < floor:
                floor += (frame_db - floor) * self.noise_floor_fall_coeff
            else:
                floor += floor_rise
            floor = min(self.noise_floor_max_db, max(self.noise_floor_min_db, floor))
            gate_db = min(floor + self.gate_margin_db, self.ceil_db - self.min_gate_span_db)
            span = max(1e-6, self.ceil_db - gate_db)
            d_target = (frame_db - gate_db) / span
            # 底部死区：把门限附近的底噪抖动压成 0（静音=平线），不影响正常说话
            dead = self.display_deadzone
            d_target = (d_target - dead) / (1.0 - dead) if dead > 0.0 else d_target
            d_target = 0.0 if d_target <= 0.0 else (1.0 if d_target >= 1.0 else d_target)
            d_coeff = self._attack_coeff if d_target > display else self._release_coeff
            display += (d_target - display) * d_coeff

            peak = max(target, peak - peak_fall)
            self._history.append(display)
        self._noise_floor_db = floor
        self._gate_db = gate_db
        self._level = max(0.0, min(1.0, level))
        self._display = max(0.0, min(1.0, display))
        self._peak = max(0.0, min(1.0, peak))
        self._raw = mapped_list[-1] if mapped_list else 0.0
        self._rms = float(rms[-1]) if rms.size else 0.0
        self._dbfs = float(dbfs_list[-1]) if dbfs_list else float("-inf")
        self._frames_total += len(mapped_list)

    def reset(self) -> None:
        """清零包络/峰值/历史（采集进程重启或流关闭时调用）。"""
        with self._lock:
            self._pending = b""
            self._level = 0.0
            self._display = 0.0
            self._raw = 0.0
            self._peak = 0.0
            self._rms = 0.0
            self._dbfs = float("-inf")
            self._noise_floor_db = None
            self._gate_db = None
            self._history.clear()

    # ---------- 读取（UI 线程） ----------

    @property
    def level(self) -> float:
        """0..1 平滑包络（规格口径：attack 15ms / release 180ms，-60dB→0/-6dB→1）。"""
        with self._lock:
            return self._level

    @property
    def display_level(self) -> float:
        """0..1 自适应噪声门后的包络（UI 用；静音=0 平线，说话明显波动）。"""
        with self._lock:
            return self._display

    @property
    def noise_floor_db(self) -> float | None:
        """自适应噪声底（dBFS），尚未收到音频时为 None。"""
        with self._lock:
            return self._noise_floor_db

    @property
    def gate_db(self) -> float | None:
        """当前显示门限（dBFS），低于它的声音显示为平线。"""
        with self._lock:
            return self._gate_db

    @property
    def raw_level(self) -> float:
        """最后一帧未平滑的 0..1 映射值（诊断用）。"""
        with self._lock:
            return self._raw

    @property
    def peak(self) -> float:
        """0..1 峰值保持（2.0/s 线性衰减）。"""
        with self._lock:
            return self._peak

    @property
    def rms(self) -> float:
        """最后一帧的 RMS 幅度（0..1）。"""
        with self._lock:
            return self._rms

    @property
    def dbfs(self) -> float:
        """最后一帧的 dBFS（静音为 -inf）。"""
        with self._lock:
            return self._dbfs

    @property
    def frames_total(self) -> int:
        """已处理的内部帧数（10ms/帧，诊断用）。"""
        with self._lock:
            return self._frames_total

    def snapshot(self) -> dict:
        """一次性取齐所有读数（避免多次加锁读到不同时刻的值）。"""
        with self._lock:
            return {
                "level": self._level,
                "display_level": self._display,
                "raw_level": self._raw,
                "peak": self._peak,
                "rms": self._rms,
                "dbfs": self._dbfs,
                "noise_floor_db": self._noise_floor_db,
                "gate_db": self._gate_db,
                "frames": self._frames_total,
            }

    def history(self, seconds: float = 0.6) -> list[float]:
        """最近若干个内部帧的 level（默认 0.6s），供波形条做相位/历史用。"""
        count = max(1, int(round(seconds / self._hop_seconds)))
        with self._lock:
            items = list(self._history)
        if count >= len(items):
            return items
        return items[-count:]
