# -*- coding: utf-8 -*-
"""voiceime.audio：基于 ffmpeg dshow 的麦克风枚举与录音（16000 Hz / mono / s16 WAV）。

对外接口（与 docs/DESIGN.md 一致）：
    list_input_devices(ffmpeg_path) -> list[str]
    resolve_device(ffmpeg_path, configured) -> str
    class AudioRecorder(ffmpeg_path, device=None, sample_rate=16000, channels=1, out_dir=None)
        start() / stop() / cancel() / active / elapsed()

实现要点：
- 设备枚举解析 ``ffmpeg -list_devices true -f dshow -i dummy`` 的 stderr；
- 录音进程以 stdin 管道交互，``stop()`` 先写 ``q`` 让 ffmpeg 完整回写 WAV 头，超时才 terminate/kill；
- ``start()`` 会等待 ffmpeg 真正打开设备并创建输出文件后才返回，避免用户开口前 1~2 秒语音丢失
  或被静默丢弃；同时能在启动阶段把“设备被占用/无麦克风”等错误以 AudioError 形式抛出；
- 所有异常均为中文可读的 AudioError。
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import wave
from pathlib import Path

LOGGER = logging.getLogger(__name__)

# ffmpeg dshow 设备行形如：
# [in#0 @ 000001c4c8592300] "麦克风阵列 (Realtek(R) Audio)" (audio)
_DEVICE_RE = re.compile(r'"([^"]+)"\s+\((audio|video)\)')
# 新建进程不弹出控制台窗口（pythonw 后台运行时必须）
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# start() 等待“输出文件出现”（= 设备已打开）的上限，冷启动时 Realtek 驱动可能耗时 1~2 秒
_STARTUP_TIMEOUT = 6.0
# stop() 等待 ffmpeg 响应 'q' 的时长；正常约 0.1~0.5 秒
_STOP_TIMEOUT = 8.0
# dshow 采集缓冲（毫秒）。实测（本机 Realtek 麦克风，2 秒录音）：
#   默认值 -> 打开 1.77s / 收尾 0.53s / 尾部多录 0.50s
#   100ms  -> 打开 0.68s / 收尾 0.12s / 尾部多录 0.10s
# 取 100ms 在启动延迟与丢帧风险之间折中。
_AUDIO_BUFFER_MS = 100


class AudioError(Exception):
    """麦克风设备或录音过程出错（消息为中文可读提示）。"""


def _decode_console(raw: bytes) -> str:
    """解码子进程控制台输出：ffmpeg 在 Windows 上输出 UTF-8，兜底 GBK。"""
    if not raw:
        return ""
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _check_ffmpeg(ffmpeg_path: str) -> str:
    """校验并返回 ffmpeg 可执行文件路径；支持直接传 PATH 中的命令名。"""
    if not ffmpeg_path:
        raise AudioError(
            "未配置 ffmpeg 路径，无法录音。请在 config.json 的 ffmpeg_path 中指定 ffmpeg.exe。"
        )
    path = Path(str(ffmpeg_path))
    if path.is_file():
        return str(path)
    found = shutil.which(str(ffmpeg_path))
    if found:
        return found
    raise AudioError(
        f"未找到 ffmpeg 可执行文件：{ffmpeg_path}。"
        "请在 config.json 的 ffmpeg_path 中指定 ffmpeg.exe 的绝对路径。"
    )


def _audio_devices_from_text(text: str) -> list[str]:
    devices: list[str] = []
    for name, kind in _DEVICE_RE.findall(text or ""):
        if kind == "audio" and name not in devices:
            devices.append(name)
    return devices


def list_input_devices(ffmpeg_path: str) -> list[str]:
    """返回 ffmpeg 能识别的 dshow 音频输入设备名列表（按 ffmpeg 枚举顺序）。"""
    ffmpeg = _check_ffmpeg(ffmpeg_path)
    cmd = [ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"]
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=30,
            creationflags=_CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired as exc:
        raise AudioError("枚举麦克风设备超时（ffmpeg 无响应），请稍后重试。") from exc
    except OSError as exc:
        raise AudioError(f"无法运行 ffmpeg 枚举麦克风设备：{exc}") from exc
    text = _decode_console(proc.stderr or b"")
    devices = _audio_devices_from_text(text)
    if devices:
        return devices
    # 无音频设备：区分“确实没有设备”和“ffmpeg 本身执行失败”
    lowered = text.lower()
    if "error opening input file dummy" in lowered or "dshow" in lowered:
        return []
    detail = " | ".join(line.strip() for line in text.splitlines() if line.strip())[-300:]
    raise AudioError(
        "ffmpeg 枚举音频设备失败（未解析到任何设备）。"
        + (f"ffmpeg 输出：{detail}" if detail else "")
    )


def resolve_device(ffmpeg_path: str, configured: str | None) -> str:
    """选择录音设备：配置名有效则用它，否则退回收到的第一个设备；没有设备则抛 AudioError。"""
    devices = list_input_devices(ffmpeg_path)
    if not devices:
        raise AudioError(
            "未检测到可用的麦克风输入设备。请确认麦克风已连接、未被禁用，"
            "并在“Windows 设置 → 隐私和安全性 → 麦克风”中允许桌面应用访问麦克风。"
        )
    if configured:
        if configured in devices:
            return configured
        LOGGER.warning(
            "配置的麦克风「%s」不在设备列表中，自动改用第一个设备「%s」。", configured, devices[0]
        )
    return devices[0]


class AudioRecorder:
    """ffmpeg dshow 录音器：输出 16k/mono/s16 WAV。

    典型用法::

        recorder = AudioRecorder(ffmpeg_path, device=None)
        recorder.start()          # 返回时 ffmpeg 已打开设备并开始采集
        ...                       # 用户说话
        wav_path = recorder.stop()  # 写 q 优雅收尾并返回 WAV 绝对路径
    """

    def __init__(
        self,
        ffmpeg_path: str,
        device: str | None = None,
        sample_rate: int = 16000,
        channels: int = 1,
        out_dir: str | None = None,
    ) -> None:
        if int(sample_rate) <= 0 or int(channels) <= 0:
            raise AudioError(f"录音参数非法：sample_rate={sample_rate}，channels={channels}。")
        self._ffmpeg_input = str(ffmpeg_path or "")
        self._ffmpeg: str | None = None
        self._device = device
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.out_dir = out_dir
        self._lock = threading.RLock()
        self._proc: subprocess.Popen | None = None
        self._path: Path | None = None
        self._stderr_lines: list[str] = []
        self._stderr_thread: threading.Thread | None = None
        self._start_monotonic: float | None = None
        self._last_elapsed = 0.0
        self._last_returncode: int | None = None

    # ------------------------------------------------------------------ 状态
    @property
    def active(self) -> bool:
        """是否正在录音（ffmpeg 进程存活）。"""
        with self._lock:
            proc = self._proc
            return proc is not None and proc.poll() is None

    @property
    def device(self) -> str | None:
        """当前/最近一次实际使用的设备名。"""
        with self._lock:
            return self._device

    def elapsed(self) -> float:
        """已录秒数（单调时钟）；未开始或已取消返回 0，stop 后返回最后一次录音时长。"""
        with self._lock:
            if self._proc is not None and self._proc.poll() is None and self._start_monotonic is not None:
                return max(0.0, time.monotonic() - self._start_monotonic)
            return self._last_elapsed

    # ------------------------------------------------------------------ 录音
    def start(self) -> None:
        """启动 ffmpeg 录音；返回时设备已打开。失败抛 AudioError。"""
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                raise AudioError("录音已在进行中，请先调用 stop() 或 cancel()。")
            self._discard_process()

        ffmpeg = _check_ffmpeg(self._ffmpeg_input)
        # 已显式给出设备名时直接使用（调用方通常已先 resolve_device，避免重复枚举 ~0.3s）；
        # 只有未指定设备时才枚举并取第一个。
        if self._device:
            device = self._device
        else:
            device = resolve_device(ffmpeg, None)

        out_dir = Path(self.out_dir) if self.out_dir else Path(tempfile.gettempdir())
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise AudioError(f"无法创建录音临时目录：{out_dir}（{exc}）。") from exc
        path = out_dir / f"rec_{uuid.uuid4().hex}.wav"

        cmd = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            # 减小 dshow 采集缓冲，显著降低“开始录音/收尾”延迟（实测见文件头注释）
            "-audio_buffer_size",
            str(_AUDIO_BUFFER_MS),
            "-f",
            "dshow",
            "-i",
            f"audio={device}",
            "-ac",
            str(self.channels),
            "-ar",
            str(self.sample_rate),
            "-c:a",
            "pcm_s16le",
            "-y",
            str(path),
        ]
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=_CREATE_NO_WINDOW,
            )
        except OSError as exc:
            raise AudioError(
                f"启动 ffmpeg 录音失败：{exc}。请检查 ffmpeg 路径与麦克风权限。"
            ) from exc

        stderr_lines: list[str] = []
        stderr_thread = threading.Thread(
            target=self._pump_stderr, args=(proc, stderr_lines), daemon=True
        )
        with self._lock:
            self._ffmpeg = ffmpeg
            self._device = device
            self._path = path
            self._proc = proc
            self._stderr_lines = stderr_lines
            self._stderr_thread = stderr_thread
            self._start_monotonic = None
            self._last_elapsed = 0.0
            self._last_returncode = None
        stderr_thread.start()

        try:
            self._await_capture(proc, path)
        except AudioError:
            with self._lock:
                self._discard_process()
            raise
        with self._lock:
            self._start_monotonic = time.monotonic()

    def stop(self) -> str:
        """正常结束录音，返回 16k/mono/s16 WAV 绝对路径；失败抛 AudioError。"""
        with self._lock:
            proc = self._proc
            path = self._path
            if proc is None or proc.poll() is not None:
                self._proc = None
                self._path = None
                raise AudioError("当前没有正在进行的录音（请先调用 start()）。")
            if self._start_monotonic is not None:
                self._last_elapsed = max(0.0, time.monotonic() - self._start_monotonic)
            # 先摘掉状态：对外 active 立即变 False，收尾期间界面不会误显示录音中
            self._proc = None
            self._path = None
        self._finish(proc)
        self._validate_wav(path)
        return str(path.resolve())

    def cancel(self) -> None:
        """结束录音并删除临时文件；不抛异常（用于 Esc 取消）。"""
        try:
            with self._lock:
                proc = self._proc
                path = self._path
                self._proc = None
                self._path = None
                self._start_monotonic = None
                self._last_elapsed = 0.0
            if proc is not None:
                self._finish_quiet(proc)
            if path is not None:
                self._unlink(path)
        except Exception:  # noqa: BLE001 - 取消路径绝不允许向上抛异常
            LOGGER.debug("取消录音时发生异常（已忽略）。", exc_info=True)

    def __del__(self) -> None:  # pragma: no cover - 解释器退出时的兜底清理
        try:
            self.cancel()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ 内部
    def _pump_stderr(self, proc: subprocess.Popen, sink: list[str]) -> None:
        """后台读取 ffmpeg stderr，避免管道写满阻塞进程；保留最近 100 行用于报错。"""
        try:
            stream = proc.stderr
            if stream is None:
                return
            for raw in iter(stream.readline, b""):
                line = _decode_console(raw).strip()
                if line:
                    sink.append(line)
                    if len(sink) > 100:
                        del sink[: len(sink) - 100]
        except (OSError, ValueError):
            pass

    def _stderr_hint(self, limit: int = 3) -> str:
        lines = [line for line in self._stderr_lines if line.strip()]
        if not lines:
            return ""
        return "；ffmpeg 输出：" + " | ".join(lines[-limit:])

    def _await_capture(self, proc: subprocess.Popen, path: Path) -> None:
        """等待 ffmpeg 打开设备并创建输出文件；进程提前退出则抛出带 ffmpeg 细节的 AudioError。"""
        deadline = time.monotonic() + _STARTUP_TIMEOUT
        while time.monotonic() < deadline:
            returncode = proc.poll()
            if returncode is not None:
                self._join_stderr(1.0)
                hint = self._stderr_hint()
                raise AudioError(
                    f"无法打开麦克风「{self._device}」（ffmpeg 退出码 {returncode}）。"
                    + hint
                    + "请确认设备未被其他程序独占，且已在 Windows 隐私设置中允许桌面应用访问麦克风。"
                )
            try:
                if path.exists():
                    return
            except OSError:
                pass
            time.sleep(0.03)
        LOGGER.warning(
            "ffmpeg 在 %.1f 秒内未确认写入录音文件，仍继续尝试录音：%s", _STARTUP_TIMEOUT, path
        )

    def _join_stderr(self, timeout: float) -> None:
        thread = self._stderr_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)

    def _close_stdin(self, proc: subprocess.Popen) -> bool:
        stdin = proc.stdin
        if stdin is None or stdin.closed:
            return False
        try:
            stdin.write(b"q")
            stdin.flush()
            stdin.close()
            return True
        except (OSError, ValueError):
            return False

    def _finish(self, proc: subprocess.Popen) -> None:
        """优雅收尾：先写 q；超时则 terminate/kill，保证 WAV 头尽量完整。"""
        self._close_stdin(proc)
        try:
            self._last_returncode = proc.wait(timeout=_STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            LOGGER.warning("ffmpeg 未响应 q 命令，强制结束录音进程。")
            self._terminate(proc)
        self._join_stderr(2.0)
        self._close_pipes(proc)

    def _finish_quiet(self, proc: subprocess.Popen) -> None:
        """取消/丢弃进程时的静默收尾，不抛异常。"""
        try:
            self._close_stdin(proc)
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self._terminate(proc)
            self._join_stderr(1.0)
            self._close_pipes(proc)
        except Exception:  # noqa: BLE001
            LOGGER.debug("ffmpeg 静默收尾异常（已忽略）。", exc_info=True)

    @staticmethod
    def _terminate(proc: subprocess.Popen) -> None:
        try:
            proc.terminate()
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                LOGGER.error("ffmpeg 进程无法结束（pid=%s）。", getattr(proc, "pid", "?"))
        except OSError:
            pass

    @staticmethod
    def _close_pipes(proc: subprocess.Popen) -> None:
        for stream in (proc.stdin, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except (OSError, ValueError):
                pass

    def _discard_process(self) -> None:
        """清理可能残留的录音进程与临时文件（调用方需持有锁）。"""
        proc = self._proc
        path = self._path
        self._proc = None
        self._path = None
        self._start_monotonic = None
        if proc is not None:
            self._finish_quiet(proc)
        if path is not None:
            self._unlink(path)

    def _validate_wav(self, path: Path | None) -> None:
        """校验 stop() 产物：WAV 可读、帧数 > 0、参数符合预期、数据完整。"""
        hint = self._stderr_hint()
        if path is None:
            raise AudioError("录音失败：未生成输出文件。" + hint)
        try:
            exists = path.is_file()
        except OSError:
            exists = False
        if not exists:
            raise AudioError(f"录音失败：未生成 WAV 文件（{path}）。" + hint)
        if path.stat().st_size <= 44:
            self._unlink(path)
            raise AudioError(f"录音失败：WAV 文件为空（{path}）。" + hint)
        try:
            with wave.open(str(path), "rb") as wav:
                channels = wav.getnchannels()
                framerate = wav.getframerate()
                sampwidth = wav.getsampwidth()
                nframes = wav.getnframes()
                raw = wav.readframes(nframes)
        except (wave.Error, EOFError, OSError) as exc:
            self._unlink(path)
            raise AudioError(f"录音文件损坏或无法读取：{path}（{exc}）。" + hint) from exc
        if nframes <= 0 or not raw:
            self._unlink(path)
            raise AudioError(
                "录音为空（0 个采样帧）：未采集到麦克风数据，可能录音时间过短或设备无输入。"
                + hint
            )
        expected_bytes = nframes * channels * sampwidth
        if len(raw) < expected_bytes:
            self._unlink(path)
            raise AudioError(
                f"录音文件不完整（WAV 头声明 {nframes} 帧，实际仅 {len(raw) // max(1, channels * sampwidth)} 帧）："
                "ffmpeg 可能被强制结束。" + hint
            )
        if framerate != self.sample_rate or channels != self.channels or sampwidth != 2:
            raise AudioError(
                f"录音格式异常：期望 {self.sample_rate}Hz/{self.channels}ch/16bit，"
                f"实际 {framerate}Hz/{channels}ch/{sampwidth * 8}bit。"
            )

    @staticmethod
    def _unlink(path: Path | None) -> None:
        if path is None:
            return
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            LOGGER.debug("删除临时录音文件失败：%s", path, exc_info=True)
