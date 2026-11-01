# -*- coding: utf-8 -*-
"""桌面悬浮麦克风指示球（tkinter，无第三方 UI 库）。

对外接口（见 docs/DESIGN.md）：
    class Indicator:
        __init__(config, on_toggle, on_quit, on_reload_model=None)
        set_state(state, message="")   # 线程安全
        run()                          # tkinter mainloop，必须在主线程调用
        quit()                         # 线程安全

所有跨线程状态都通过 queue.Queue 投递，由 tk 主线程 root.after(50) 轮询消费，
不存在跨线程操作 tkinter 对象的情况。

显示方式（v3）：
- 首选 **Win32 逐像素 alpha 分层窗口**（voiceime/winlayer.py + UpdateLayeredWindow）：
  悬浮球真半透明、能透出桌面壁纸，球外 alpha=0 区域点击穿透；
- 分层窗口不可用时自动回退到旧 **透明色键** 方案（RGBA 合成到 #0d1016 + alpha 阈值贴图）；
- 三套外观 A 苹果风毛玻璃 / E 极简幽灵 / F 深色磨砂，右键「外观」子菜单实时切换，
  选择写入 config["ui"]["look"]，退出时保存。环境变量 VOICEIME_UI_LOOK / 
  VOICEIME_UI_FORCE_KEYED 仅供调试与截图取证，不改变配置语义。
"""
from __future__ import annotations

import ctypes
import logging
import math
import os
import queue
import threading
import time
import tkinter as tk
from collections import deque
from pathlib import Path

import numpy as np
from PIL import Image, ImageTk

from . import glass2, waveform
from .config import (
    APP_DIR,
    LOG_DIR,
    mark_privacy_notice_shown,
    save_config,
    should_show_privacy_notice,
)
from .winlayer import LayeredWindow, LayeredWindowError, get_top_hwnd, is_layered

logger = logging.getLogger(__name__)


# 悬浮球基础尺寸（像素，最终尺寸 = 基础尺寸 × config["ui"]["scale"]）
BALL_BASE_PX = 56
# 主循环轮询间隔（毫秒）
POLL_MS = 50
# 消息气泡显示时长（毫秒）与淡出步长
BUBBLE_SHOW_MS = 2000
BUBBLE_FADE_STEP_MS = 40
# 透明色键（回退方案用：窗口背景色，抠出圆形悬浮球）
TRANSPARENT_KEY = "#0d1016"
TRANSPARENT_KEY_RGB = (13, 16, 22)
# 三套可选外观（右键「外观」子菜单）：配置里的短代码 -> glass2 渲染方案名
LOOK_VARIANTS = {"A": "light_frost", "E": "ghost", "F": "dark_frost"}
LOOK_LABELS = {
    "A": "A 苹果风毛玻璃",
    "E": "E 极简幽灵",
    "F": "F 深色磨砂",
}
DEFAULT_LOOK = "A"
# 仅供调试/截图取证用的环境变量（不写入配置）：
#   VOICEIME_UI_LOOK=A|E|F     强制初始外观（截图脚本用来分别取三套外观）
#   VOICEIME_UI_FORCE_KEYED=1  强制走色键回退方案（验证回退路径）
#   VOICEIME_UI_NO_BUBBLE=1    不显示消息气泡（截图取证时只拍球体状态）
ENV_LOOK = "VOICEIME_UI_LOOK"
ENV_FORCE_KEYED = "VOICEIME_UI_FORCE_KEYED"
ENV_NO_BUBBLE = "VOICEIME_UI_NO_BUBBLE"
# 色键回退时保留的最小 alpha（低于阈值的像素直接抠掉）
KEYED_ALPHA_THRESHOLD = 110
# 幽灵外观的玻璃底 alpha 只有 12~36，回退色键模式需要放大 alpha 才不会整球被抠空
GHOST_ALPHA_BOOST = 3.0
GHOST_KEYED_THRESHOLD = 20

# 单击/拖动判定：按下后移动超过该距离视为拖动（像素）
CLICK_MOVE_THRESHOLD_PX = 4
# 双击去抖（秒）：两次单击间隔小于该值只算一次（避免双击误触发开始+停止）
CLICK_DEBOUNCE_S = 0.25
# 周期性把悬浮球重新抬到最顶层（毫秒）：Windows 通知/隐私提示也是 topmost，
# 会把球盖住并吃掉点击，定期重抬可保证球始终可点。
TOPMOST_REFRESH_MS = 2000
# 首次启动隐私说明气泡停留时长（毫秒）：第一次启动只弹一次，给用户读完的时间
PRIVACY_NOTICE_MS = 9000
# 隐私说明气泡在窗口启动后延迟多少毫秒再弹（等悬浮球和气泡窗口就绪）
PRIVACY_NOTICE_DELAY_MS = 600
# 右键菜单里与隐私/数据相关的命令：(菜单文案, Indicator 方法名)。
# 集中成常量便于测试断言菜单项与回调都存在（不依赖创建 Tk 窗口）。
EXTRA_MENU_COMMANDS = (
    ("打开数据目录", "_menu_open_data_dir"),
    ("清空日志", "_menu_clear_logs"),
)
# 状态动画帧间隔（毫秒），14fps 足够表达呼吸/旋转
FRAME_MS = 70
# 录音态实时声纹（ui.waveform，默认开）：每帧间隔 ~33ms（30fps）
WAVE_MS = 33
# UI 侧保留最近 WAVE_HISTORY 帧声压级（约 0.9s），供波形条错相/历史用
WAVE_HISTORY = 27
# 声纹帧耗时日志：首帧必打，之后每 WAVE_LOG_EVERY 帧汇总一次（性能证据）
WAVE_LOG_EVERY = 60

STATES = ("idle", "recording", "transcribing", "error", "loading")

STATE_COLORS = {
    "idle": "#6f7783",
    "recording": "#e5484d",
    "transcribing": "#d9a326",
    "error": "#dc2626",
    "loading": "#3b82f6",
}

STATE_LABELS = {
    "idle": "空闲",
    "recording": "录音中",
    "transcribing": "识别中",
    "error": "出错",
    "loading": "加载中",
}

MIC_COLOR = "#ffffff"
BUBBLE_BG = "#1f2430"
BUBBLE_FG = "#f2f4f8"

GWLP_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080


def _blend(color_a: str, color_b: str, ratio: float) -> str:
    """在两个 #rrggbb 颜色之间线性插值，ratio=0 返回 color_a。"""
    ratio = max(0.0, min(1.0, ratio))
    a = color_a.lstrip("#")
    b = color_b.lstrip("#")
    parts = []
    for index in range(3):
        va = int(a[index * 2:index * 2 + 2], 16)
        vb = int(b[index * 2:index * 2 + 2], 16)
        parts.append(f"{int(round(va + (vb - va) * ratio)):02x}")
    return "#" + "".join(parts)


def _short_message(message: str, limit: int = 12) -> str:
    """压缩空白并把消息截断到 limit 个字符（超出用省略号，总长仍为 limit）。"""
    text = " ".join(str(message).split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _multiline_message(message: str) -> str:
    """多行提示（首次启动隐私说明等）：保留换行、去掉空行，不截断。"""
    lines = [line.strip() for line in str(message).splitlines()]
    return "\n".join(line for line in lines if line)


def privacy_notice_text(log_dir: str | os.PathLike | None = None) -> str:
    """首次启动隐私说明文案：音频不出本机 / 识别文字默认不写日志 / 日志目录。"""
    directory = Path(log_dir) if log_dir is not None else Path(LOG_DIR)
    return (
        "隐私说明（只显示一次）：\n"
        "· 音频只在本机处理，不联网\n"
        "· 识别文字默认不写日志\n"
        "· 日志目录：" + str(directory)
    )


def clear_app_log(log_dir: str | os.PathLike | None = None,
                  logger: logging.Logger | None = None) -> int:
    """清空程序日志 ``<log_dir>/app.log``（含 RotatingFileHandler 轮转备份），返回清空前字节数。

    隐私：日志可能残留排查信息，右键「清空日志」让用户一键消除。
    句柄处理：Windows 下 RotatingFileHandler 以 append 模式持有文件句柄，直接截断会让
    下一次写入落在旧偏移处、留下 NUL 空洞；这里先 flush 所有指向该文件的 handler，
    截断到 0 字节，再把流位置归零——append 模式的下次写入自然落在文件末尾（无空洞），
    且不重建 handler，日志级别/格式保持不变。截断期间持有 handler 锁，避免并发写。
    """
    directory = Path(log_dir) if log_dir is not None else Path(LOG_DIR)
    log_path = directory / "app.log"
    log = logger if logger is not None else logging.getLogger()
    target = os.path.normcase(os.path.abspath(str(log_path)))
    try:
        previous = log_path.stat().st_size
    except OSError:
        previous = 0
    handles = []
    for handler in list(log.handlers):
        base = getattr(handler, "baseFilename", None)
        if base and os.path.normcase(os.path.abspath(str(base))) == target:
            handles.append(handler)
    for handler in handles:
        handler.acquire()
    try:
        for handler in handles:
            try:
                handler.flush()
            except Exception:  # noqa: BLE001 - flush 失败不影响截断
                pass
        directory.mkdir(parents=True, exist_ok=True)
        with open(log_path, "w", encoding="utf-8"):
            pass
        for handler in handles:
            stream = getattr(handler, "stream", None)
            if stream is not None:
                try:
                    stream.seek(0)
                except (OSError, ValueError):
                    pass
        # 轮转备份（app.log.1 / app.log.2 …）同样含旧内容，一并清掉
        for backup in sorted(directory.glob("app.log.*")):
            try:
                backup.unlink()
            except OSError:
                pass
    finally:
        for handler in reversed(handles):
            handler.release()
    return previous


def _normalize_look(value) -> str:
    """把配置里的外观值规范化成 A/E/F 短代码；非法值退回默认 A。"""
    text = str(value or "").strip().upper()
    return text if text in LOOK_VARIANTS else DEFAULT_LOOK


# Windows 默认定时器精度 15.6ms：Tk 的 after(33) 实际会变成 ~46ms（实测 21fps）。
# 录音声纹期间临时 timeBeginPeriod(1)，让 33ms ≈ 30fps（实测 30.1fps）；停止时恢复。
_TIMER_PRECISION_ACTIVE = False


def _begin_timer_precision() -> None:
    """录音声纹期间提高系统定时器精度（1ms），保证 Tk after(33) ≈ 30fps。"""
    global _TIMER_PRECISION_ACTIVE
    if _TIMER_PRECISION_ACTIVE:
        return
    try:
        ctypes.WinDLL("winmm").timeBeginPeriod(1)
        _TIMER_PRECISION_ACTIVE = True
    except Exception:  # noqa: BLE001 - 失败只是帧率降到 ~21fps，不影响功能
        logger.debug("timeBeginPeriod(1) 失败（声纹帧率可能只有 ~21fps）", exc_info=True)


def _end_timer_precision() -> None:
    """录音结束恢复系统默认定时器精度（省电、不长期影响其它程序）。"""
    global _TIMER_PRECISION_ACTIVE
    if not _TIMER_PRECISION_ACTIVE:
        return
    try:
        ctypes.WinDLL("winmm").timeEndPeriod(1)
    except Exception:  # noqa: BLE001
        logger.debug("timeEndPeriod(1) 失败", exc_info=True)
    _TIMER_PRECISION_ACTIVE = False


def _compose_keyed(rgba: Image.Image, variant: str) -> Image.Image:
    """回退色键方案：RGBA 合成到 #0d1016 底、低于 alpha 阈值的像素抠成键色。

    做法：先按阈值做硬 mask 再贴到键色底上，
    避免半透明边缘和深色键底混合产生一圈脏黑边。幽灵外观的玻璃底 alpha 极低
    （12~36），先放大 alpha 再贴图，否则整球会被抠空只剩麦克风。
    """
    arr = np.asarray(rgba.convert("RGBA"), dtype=np.uint8)
    alpha = arr[:, :, 3].astype(np.float32)
    threshold = KEYED_ALPHA_THRESHOLD
    if variant == "ghost":
        alpha = np.clip(alpha * GHOST_ALPHA_BOOST, 0, 255)
        threshold = GHOST_KEYED_THRESHOLD
    mask = Image.fromarray(((alpha >= threshold).astype(np.uint8)) * 255, "L")
    keyed = Image.new("RGB", rgba.size, TRANSPARENT_KEY_RGB)
    keyed.paste(rgba.convert("RGB"), (0, 0), mask)
    return keyed


class Indicator:
    """无边框置顶悬浮球：显示状态、接收拖动、右键菜单、消息气泡。"""

    def __init__(self, config: dict, on_toggle=None, on_quit=None, on_reload_model=None):
        self._config = config if isinstance(config, dict) else {}
        self._on_toggle = on_toggle
        self._on_quit = on_quit
        self._on_reload_model = on_reload_model

        ui_cfg = self._config.get("ui") if isinstance(self._config.get("ui"), dict) else {}
        try:
            scale = float(ui_cfg.get("scale") or 1.0)
        except (TypeError, ValueError):
            scale = 1.0
        self._scale = max(0.6, min(2.5, scale))
        self._size = max(28, int(round(BALL_BASE_PX * self._scale)))
        self._opacity = 0.92
        try:
            self._opacity = max(0.3, min(1.0, float(ui_cfg.get("opacity") or 0.92)))
        except (TypeError, ValueError):
            self._opacity = 0.92
        self._always_on_top = bool(ui_cfg.get("always_on_top", True))
        self._look = _normalize_look(ui_cfg.get("look"))
        force_look = os.environ.get(ENV_LOOK, "").strip().upper()
        if force_look in LOOK_VARIANTS:  # 调试/截图取证：强制初始外观，不写配置
            self._look = force_look
        # 调试/截图取证：VOICEIME_UI_NO_BUBBLE=1 时不显示消息气泡
        self._show_bubbles = os.environ.get(ENV_NO_BUBBLE, "").strip().lower() not in (
            "1", "true", "yes")

        self._queue: queue.Queue = queue.Queue()
        self._root: tk.Tk | None = None
        self._canvas: tk.Canvas | None = None
        self._items: dict[str, int] = {}
        self._bubble: tk.Toplevel | None = None
        self._bubble_label: tk.Label | None = None
        self._menu: tk.Menu | None = None
        self._look_menu: tk.Menu | None = None
        self._look_var: tk.StringVar | None = None

        self._state = "idle"
        self._anim_t = 0.0
        self._transparent = False
        self._pixel_scale = 1.0
        # 外观缓存：look 短代码 -> state -> 帧列表（分层模式是 PIL RGBA，回退模式是 PhotoImage）
        self._frames: dict[str, dict[str, list]] = {}
        self._frame_key: tuple | None = None
        self._use_layer = False
        self._layer: LayeredWindow | None = None
        self._pos = (0, 0)
        self._top_hwnd: int | None = None
        self._drag_origin: tuple[int, int, int, int] | None = None
        self._press_point: tuple[int, int] | None = None
        self._dragged = False
        self._last_click_at = 0.0
        self._closing = False
        self._poll_job = None
        self._raise_job = None
        self._privacy_notice_job = None
        self._bubble_hide_job = None
        self._bubble_fade_job = None
        self._bubble_fade_alpha = 0.96

        # -------- 录音态实时声纹（ui.waveform） --------
        # 开关：配置 ui.waveform 默认 true；右键菜单可勾选，退出时随 config 落盘。
        self._waveform_enabled = bool(ui_cfg.get("waveform", True))
        # 本轮录音是否提供声压级（app 在按需 WAV 回退时置 False，此时保留麦克风帧）
        self._wave_available = True
        self._level = 0.0  # app 录音线程写入（float 赋值在 CPython 下原子，读侧无需锁）
        self._level_hist: deque[float] = deque(maxlen=WAVE_HISTORY)
        self._wave_base: dict[str, Image.Image] = {}  # look -> 无麦克风底座（PIL RGBA）
        self._wave_job = None
        self._wave_var: tk.BooleanVar | None = None
        self._wave_frames = 0
        self._wave_render_ms = 0.0
        self._wave_compose_ms = 0.0
        self._wave_update_ms = 0.0
        self._wave_started_at = 0.0
        self.waveform_frame_count = 0  # 累计渲染帧数（供自检/测试取证）

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def set_state(self, state: str, message: str = "", bubble_ms: int | None = None) -> None:
        """线程安全地更新状态；消息非空时在球旁显示气泡（默认 2 秒后淡出）。

        bubble_ms：气泡停留毫秒数（None=默认 BUBBLE_SHOW_MS）。冷启动过场/就绪提示
        用更长的 loading 提示、更短的 ready 提示，避免一闪而过或一直挡着。
        """
        self._queue.put(("state", state, message, bubble_ms))

    def set_level(self, level: float) -> None:
        """线程安全：推一帧录音声压级 0..1（app 录音线程 ~30fps 调用）。

        只做一次 float 赋值，不碰 tk 对象、不投队列（30fps 下队列会堆积）；
        声纹定时器在 tk 主线程按 30fps 读取该值渲染。越界值裁剪到 0..1。
        """
        try:
            value = float(level)
        except (TypeError, ValueError):
            return
        self._level = 0.0 if value < 0.0 else (1.0 if value > 1.0 else value)

    def set_level_available(self, available: bool) -> None:
        """线程安全：告知本轮录音是否有声压级源（按需 WAV 回退时没有 → 保持麦克风帧）。"""
        self._wave_available = bool(available)

    @property
    def waveform_active(self) -> bool:
        """当前是否正在用声纹渲染（供自检/测试断言单源渲染）。"""
        return self._wave_job is not None

    def quit(self) -> None:
        """线程安全地关闭指示器并结束 mainloop。"""
        self._queue.put(("quit", "", ""))

    @property
    def state(self) -> str:
        return self._state

    def run(self) -> None:
        """创建窗口并进入 tkinter mainloop（必须在主线程调用）。"""
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("Indicator.run() 必须在主线程调用（tkinter 限制）。")
        if self._root is not None:
            raise RuntimeError("Indicator.run() 只能调用一次。")

        root = tk.Tk()
        self._root = root
        root.title("voice-ime")
        root.overrideredirect(True)
        root.resizable(False, False)
        # DPI 感知后按物理像素渲染，图标才能锐利（否则 125% 下会被系统放大发虚）
        try:
            self._pixel_scale = max(1.0, float(root.winfo_fpixels("1i")) / 96.0)
        except tk.TclError:
            self._pixel_scale = 1.0
        self._size = max(28, int(round(BALL_BASE_PX * self._scale * self._pixel_scale)))
        try:
            root.attributes("-topmost", self._always_on_top)
        except tk.TclError:
            logger.debug("设置置顶属性失败", exc_info=True)

        canvas = tk.Canvas(
            root, width=self._size, height=self._size,
            bg=TRANSPARENT_KEY, highlightthickness=0, bd=0, takefocus=0,
        )
        canvas.pack(fill="both", expand=True)
        canvas.configure(cursor="hand2")
        self._canvas = canvas
        self._items["ball"] = canvas.create_image(
            self._size / 2, self._size / 2, image=None
        )
        self._place_window()
        self._bind_events()
        self._build_menu()
        self._prepare_windows()
        self._init_render_mode()
        self._ensure_look_frames(self._look)

        self._poll_job = root.after(POLL_MS, self._poll)
        self._raise_job = root.after(TOPMOST_REFRESH_MS, self._keep_topmost)
        # 首次启动隐私说明：延迟一点等悬浮球就绪，只弹一次
        self._privacy_notice_job = root.after(
            PRIVACY_NOTICE_DELAY_MS, self._maybe_show_privacy_notice
        )
        logger.info(
            "指示器窗口已启动：%dx%d，外观 %s（%s），真透明分层=%s",
            self._size, self._size, self._look, LOOK_VARIANTS[self._look],
            self._use_layer,
        )
        try:
            root.mainloop()
        finally:
            self._root = None
            self._canvas = None
            self._closing = True
            logger.info("指示器 mainloop 已退出")

    # ------------------------------------------------------------------
    # 窗口初始化
    # ------------------------------------------------------------------
    def _setup_transparency(self) -> None:
        """回退方案：Windows 下用透明色键把方形窗口抠成圆形悬浮球。"""
        try:
            self._root.attributes("-transparentcolor", TRANSPARENT_KEY)
            self._transparent = True
            logger.info("窗口透明方式：色键回退（%s）", TRANSPARENT_KEY)
        except tk.TclError:
            self._transparent = False
            logger.warning("当前 Tk 不支持 -transparentcolor，使用方形背景兜底")

    def _init_render_mode(self) -> None:
        """优先启用真 alpha 分层窗口；不可用时回退透明色键方案（不黑屏、不崩溃）。"""
        self._layer = None
        self._use_layer = False
        forced = os.environ.get(ENV_FORCE_KEYED, "").strip().lower() in ("1", "true", "yes")
        if forced:
            logger.warning("按 %s 强制使用色键回退方案（调试用）", ENV_FORCE_KEYED)
        elif self._top_hwnd:
            try:
                self._layer = LayeredWindow(self._top_hwnd)
                self._use_layer = True
                logger.info(
                    "真透明分层窗口已启用：HWND=0x%X，WS_EX_LAYERED=%s"
                    "（UpdateLayeredWindow 逐像素 alpha）",
                    self._top_hwnd, is_layered(self._top_hwnd),
                )
                return
            except LayeredWindowError as exc:
                logger.warning("真透明分层窗口不可用，回退色键方案：%s", exc)
                self._layer = None
            except Exception:  # noqa: BLE001 - 任何异常都必须能回退，不能黑屏
                logger.warning("真透明分层窗口初始化异常，回退色键方案", exc_info=True)
                self._layer = None
        else:
            logger.warning("未拿到顶层窗口句柄，回退色键方案")
        self._setup_transparency()
        try:
            self._root.attributes("-alpha", self._opacity)
        except tk.TclError:
            logger.debug("设置窗口透明度失败", exc_info=True)

    def _ensure_look_frames(self, look: str) -> None:
        """渲染并缓存某套外观的全部状态帧；先出 idle 帧，其余状态随后补齐。"""
        variant = LOOK_VARIANTS.get(look)
        if variant is None:
            return
        cached = self._frames.setdefault(look, {})
        started = time.perf_counter()
        for state in glass2.FRAME_COUNTS:
            if cached.get(state):
                continue
            try:
                images = glass2.render_frames(variant, self._size, state)
            except Exception:  # noqa: BLE001 - 单个状态渲染失败不影响其它状态
                logger.exception("渲染悬浮球帧失败：外观 %s 状态 %s", look, state)
                continue
            if self._use_layer:
                cached[state] = list(images)
            else:
                cached[state] = self._to_photos(images, variant, state)
            if state == "idle" and look == self._look:
                self._frame_key = None
                self._apply_frame(force=True)
        total = sum(len(frames) for frames in cached.values())
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        logger.info(
            "玻璃球帧已加载：外观 %s（%s）%d 帧，%.0f ms，真透明分层=%s",
            look, variant, total, elapsed_ms, self._use_layer,
        )
        # 录音声纹底座（无麦克风）提前渲染并缓存，避免开始录音那一瞬卡 30~40ms
        if self._waveform_enabled:
            self._ensure_wave_base(look)

    def _to_photos(self, images: list[Image.Image], variant: str, state: str) -> list:
        """把 RGBA 帧转成色键 PhotoImage（回退方案用）。"""
        photos = []
        temp_dir = None
        for index, image in enumerate(images):
            keyed = _compose_keyed(image, variant)
            try:
                photos.append(ImageTk.PhotoImage(keyed, master=self._root))
                continue
            except Exception:  # noqa: BLE001 - ImageTk 不可用时落盘再读
                logger.debug("ImageTk 不可用，改用 PNG 文件帧", exc_info=True)
            try:
                if temp_dir is None:
                    import tempfile
                    from pathlib import Path

                    temp_dir = Path(tempfile.gettempdir()) / f"voice-ime-keyed-{self._size}"
                    temp_dir.mkdir(parents=True, exist_ok=True)
                path = temp_dir / f"{self._look}_{state}_{index}.png"
                keyed.save(path)
                photos.append(tk.PhotoImage(file=str(path), master=self._root))
            except Exception:  # noqa: BLE001
                logger.exception("加载色键帧失败：%s/%s", state, index)
                break
        return photos

    def _place_window(self) -> None:
        ui_cfg = self._config.get("ui") if isinstance(self._config.get("ui"), dict) else {}
        screen_w = self._root.winfo_screenwidth()
        screen_h = self._root.winfo_screenheight()
        pos_x = ui_cfg.get("pos_x")
        pos_y = ui_cfg.get("pos_y")
        if isinstance(pos_x, int) and isinstance(pos_y, int):
            x, y = pos_x, pos_y
        else:
            # 默认放在右侧边缘、垂直约 42% 处：避开右下角的 Windows 通知/隐私提示
            # （麦克风启用时系统会在右下角弹"隐私设备保护"提示，老位置会互相遮挡）。
            x = screen_w - self._size - 24
            y = int(screen_h * 0.42)
        x = max(0, min(x, screen_w - self._size))
        y = max(0, min(y, screen_h - self._size))
        self._root.geometry(f"{self._size}x{self._size}+{x}+{y}")
        # 分层窗口的 UpdateLayeredWindow 由我们自己按物理屏幕坐标定位，记下权威坐标
        self._pos = (int(x), int(y))

    def _bind_events(self) -> None:
        canvas = self._canvas
        canvas.bind("<ButtonPress-1>", self._on_drag_start)
        canvas.bind("<B1-Motion>", self._on_drag_move)
        canvas.bind("<ButtonRelease-1>", self._on_drag_end)
        canvas.bind("<Button-3>", self._on_right_click)
        canvas.bind("<Enter>", lambda _event: self._raise_window())
        self._root.bind("<Button-3>", self._on_right_click)

    def _build_menu(self) -> None:
        menu = tk.Menu(self._root, tearoff=0)
        menu.add_command(label="开始语音输入", command=self._menu_toggle)
        menu.add_command(label="重新加载模型", command=self._menu_reload)
        # 录音声纹开关（ui.waveform）：录音时用实时声纹替代麦克风图标
        self._wave_var = tk.BooleanVar(master=self._root, value=self._waveform_enabled)
        menu.add_checkbutton(
            label="录音声纹", variable=self._wave_var, command=self._menu_toggle_waveform,
        )
        # 外观子菜单：A/E/F 三套 radio 单选项，当前项勾选，点击立即换装
        look_menu = tk.Menu(menu, tearoff=0)
        self._look_var = tk.StringVar(master=self._root, value=self._look)
        for code in ("A", "E", "F"):
            look_menu.add_radiobutton(
                label=LOOK_LABELS[code],
                variable=self._look_var,
                value=code,
                command=lambda c=code: self._menu_set_look(c),
            )
        menu.add_cascade(label="外观", menu=look_menu)
        menu.add_separator()
        # 隐私/数据命令：打开程序目录、清空日志（文案与方法名见 EXTRA_MENU_COMMANDS）
        for label, method_name in EXTRA_MENU_COMMANDS:
            menu.add_command(label=label, command=getattr(self, method_name))
        menu.add_separator()
        menu.add_command(label="退出", command=self._menu_quit)
        self._menu = menu
        self._look_menu = look_menu

    def _maybe_show_privacy_notice(self) -> None:
        """首次启动弹一次隐私说明气泡，随后把 ui.privacy_notice_shown 写成 true。"""
        self._privacy_notice_job = None
        try:
            if not should_show_privacy_notice(self._config):
                return
            self._show_message(
                privacy_notice_text(), duration_ms=PRIVACY_NOTICE_MS, multiline=True
            )
            # 弹成功后再标记（退出时随 config.json 落盘），之后不再弹
            mark_privacy_notice_shown(self._config)
            logger.info(
                "首次启动：已显示隐私说明气泡（音频不出本机 / 识别文字默认不写日志 / 日志目录）"
            )
        except Exception:  # noqa: BLE001 - 隐私提示失败不能影响主流程
            logger.exception("显示隐私说明失败")

    def _prepare_windows(self) -> None:
        """把窗口设为不抢焦点（避免点击悬浮球导致当前输入框失焦）。"""
        self._root.update_idletasks()
        self._apply_noactivate(self._root.winfo_id())

    def _apply_noactivate(self, child_hwnd: int) -> bool:
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.GetParent.argtypes = [ctypes.c_void_p]
            user32.GetParent.restype = ctypes.c_void_p
            user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
            user32.GetWindowLongW.restype = ctypes.c_long
            user32.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
            user32.SetWindowLongW.restype = ctypes.c_long
            hwnd = int(child_hwnd)
            parent = user32.GetParent(hwnd) or hwnd
            style = int(user32.GetWindowLongW(parent, GWLP_EXSTYLE))
            user32.SetWindowLongW(
                parent, GWLP_EXSTYLE, style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
            )
            self._top_hwnd = int(parent)
            return True
        except Exception:
            logger.debug("设置窗口不抢焦点失败（不影响拖动与显示）", exc_info=True)
            return False

    def _keep_topmost(self) -> None:
        """周期性把球重新抬到最顶层，避免被 Windows 通知/隐私提示遮挡而点不到。"""
        self._raise_job = None
        self._raise_window()
        if self._root is not None and not self._closing:
            try:
                self._raise_job = self._root.after(TOPMOST_REFRESH_MS, self._keep_topmost)
            except tk.TclError:
                pass

    def _raise_window(self) -> None:
        hwnd = getattr(self, "_top_hwnd", None)
        if self._root is not None and hwnd:
            try:
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                user32.SetWindowPos.argtypes = [
                    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
                    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint,
                ]
                user32.SetWindowPos.restype = ctypes.c_int
                # HWND_TOPMOST=-1；SWP_NOMOVE|SWP_NOSIZE|SWP_NOACTIVATE
                # 注意：不要加 SWP_SHOWWINDOW —— 分层窗口（WS_EX_LAYERED+ULW）下它会把
                # 已 withdraw 的 owned 窗口（消息气泡）重新显示，留下半透明残影（实测回归）。
                user32.SetWindowPos(int(hwnd), -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
            except Exception:
                logger.debug("重新置顶失败（忽略）", exc_info=True)
            try:
                self._root.lift()
            except tk.TclError:
                pass

    # ------------------------------------------------------------------
    # 主线程轮询：消费跨线程事件 + 动画
    # ------------------------------------------------------------------
    def _poll(self) -> None:
        self._poll_job = None
        if self._root is None:
            return
        should_quit = False
        notify_app = False
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            except ValueError:
                continue
            kind = item[0] if item else ""
            state = item[1] if len(item) > 1 else ""
            message = item[2] if len(item) > 2 else ""
            bubble_ms = item[3] if len(item) > 3 else None
            if kind == "state":
                self._apply_state(state, message, bubble_ms)
            elif kind == "quit":
                should_quit = True
            elif kind == "menu_quit":
                should_quit = True
                notify_app = True
        if should_quit:
            self._shutdown(notify_app=notify_app)
            return
        self._tick()
        self._poll_job = self._root.after(POLL_MS, self._poll)

    def _apply_state(self, state: str, message: str, bubble_ms: int | None = None) -> None:
        if state not in STATE_COLORS:
            logger.warning("未知的指示器状态：%r（已按 idle 处理）", state)
            state = "idle"
        if state != self._state:
            logger.info("指示器状态 -> %s", state)
        self._state = state
        # 录音态：有分层窗口 + 声压级源时切到实时声纹；其余状态恢复麦克风帧。
        if state == "recording" and self._waveform_allowed():
            self._start_wave()
        elif self._wave_job is not None:
            self._stop_wave(reset_frame=True)
        if message:
            self._show_message(message, duration_ms=bubble_ms)

    def _tick(self) -> None:
        """按当前状态切换预渲染帧（呼吸/闪烁/旋转由帧序列表达）。"""
        if self._canvas is None:
            return
        self._anim_t += POLL_MS / 1000.0
        self._apply_frame()
        # 提示气泡跟随悬浮球移动（气泡淡出结束时会被销毁，不会残留）
        if self._bubble is not None and self._bubble.winfo_viewable():
            self._position_bubble()

    def _current_frames(self) -> tuple[list, str]:
        """返回 (当前状态帧列表, 实际使用的状态名)；该状态帧未就绪时用 idle 兜底。"""
        look_frames = self._frames.get(self._look) or {}
        state = self._state
        frames = look_frames.get(state)
        if frames:
            return frames, state
        return look_frames.get("idle") or [], "idle"

    def _apply_frame(self, force: bool = False) -> None:
        """把当前帧送到分层窗口 / Canvas（仅帧索引变化时才更新，14fps）。"""
        # 单源渲染：录音声纹激活时由 _wave_tick 独占分层窗口，这里一律不画，
        # 否则会出现「底座/麦克风帧 与 声纹帧」两套帧交替、投影像素错位/叠加的问题。
        if self._wave_active():
            return
        frames, used_state = self._current_frames()
        if not frames:
            return
        index = int(self._anim_t * 1000.0 / FRAME_MS) % len(frames)
        key = (self._look, used_state, index, self._use_layer)
        if not force and key == self._frame_key:
            return
        if self._use_layer:
            layer = self._layer
            if layer is None:
                return
            try:
                layer.update(frames[index], self._pos[0], self._pos[1])
            except Exception as exc:  # noqa: BLE001 - 分层渲染失败必须能回退，不能黑屏
                self._switch_to_keyed(exc)
                return
        else:
            try:
                self._canvas.itemconfigure(self._items["ball"], image=frames[index])
            except tk.TclError:
                return
        self._frame_key = key

    def _switch_to_keyed(self, reason: Exception) -> None:
        """运行中分层窗口更新失败：切换到色键回退方案并继续显示（不黑屏）。"""
        if not self._use_layer:
            return
        # 声纹只在真透明分层模式下有意义：先停掉声纹定时器并清底座
        self._stop_wave(reset_frame=False)
        self._wave_base.clear()
        logger.warning("分层窗口更新失败，回退色键方案：%s", reason)
        try:
            if self._layer is not None:
                self._layer.dispose()
        except Exception:  # noqa: BLE001
            logger.debug("释放分层窗口失败（忽略）", exc_info=True)
        self._layer = None
        self._use_layer = False
        self._setup_transparency()
        try:
            self._root.attributes("-alpha", self._opacity)
        except tk.TclError:
            pass
        # 已缓存的 PIL 帧就地转成色键 PhotoImage
        for look, states in self._frames.items():
            variant = LOOK_VARIANTS.get(look, "light_frost")
            for state, frames in list(states.items()):
                if not frames or not isinstance(frames[0], Image.Image):
                    continue
                try:
                    states[state] = self._to_photos(frames, variant, state)
                except Exception:  # noqa: BLE001
                    logger.exception("回退色键方案时转换帧失败：%s/%s", look, state)
                    states[state] = []
        self._frame_key = None
        self._apply_frame(force=True)

    # ------------------------------------------------------------------
    # 录音态实时声纹（recording：麦克风图标消失 → 实时声纹）
    # ------------------------------------------------------------------
    def _waveform_allowed(self) -> bool:
        """声纹是否需要且能够渲染：配置开启 + 真透明分层模式 + 本轮有声压级源。"""
        return bool(
            self._waveform_enabled
            and self._wave_available
            and self._use_layer
            and self._layer is not None
            and self._root is not None
        )

    def _wave_active(self) -> bool:
        """声纹是否正在驱动显示（单源渲染判据）。"""
        return bool(self._wave_job is not None and self._use_layer and self._state == "recording")

    def _ensure_wave_base(self, look: str) -> Image.Image:
        """渲染并缓存「无麦克风」玻璃底座（每个外观只渲染一次，实测 29~42ms）。"""
        base = self._wave_base.get(look)
        if base is not None:
            return base
        variant = LOOK_VARIANTS.get(look)
        if variant is None:
            raise ValueError("未知外观：%r" % (look,))
        started = time.perf_counter()
        base = glass2.render_base(variant, self._size, state="recording")
        self._wave_base[look] = base
        logger.info(
            "录音声纹底座已渲染：外观 %s（%s）%dx%d，%.0f ms",
            look, variant, self._size, self._size,
            (time.perf_counter() - started) * 1000.0,
        )
        return base

    def _start_wave(self) -> None:
        """录音开始：进入声纹渲染（幂等；底座已在启动/切外观时预渲染）。"""
        if self._wave_job is not None or not self._waveform_allowed():
            return
        try:
            self._ensure_wave_base(self._look)
        except Exception:  # noqa: BLE001 - 声纹失败不能让录音流程不可用
            logger.exception("录音声纹底座渲染失败，本次录音保留麦克风动画")
            self._wave_available = False
            return
        # 预热历史：避免第一帧因为历史为空而形状跳一下（level=0 时仍是平线）
        self._level_hist.clear()
        self._level_hist.extend([0.0] * WAVE_HISTORY)
        self._wave_frames = 0
        self._wave_render_ms = 0.0
        self._wave_compose_ms = 0.0
        self._wave_update_ms = 0.0
        self._wave_started_at = time.monotonic()
        self._frame_key = None
        logger.info(
            "录音声纹已启用：%dx%d，底座外观 %s（%s），目标 30fps",
            self._size, self._size, self._look, LOOK_VARIANTS[self._look],
        )
        _begin_timer_precision()  # Windows 默认 15.6ms 定时精度会把 33ms 拉成 46ms
        self._wave_tick()

    def _stop_wave(self, reset_frame: bool = True) -> None:
        """退出录音/关闭声纹：停定时器，打印帧耗时统计，并恢复麦克风帧显示。"""
        _end_timer_precision()
        job, self._wave_job = self._wave_job, None
        if job is not None and self._root is not None:
            try:
                self._root.after_cancel(job)
            except (tk.TclError, ValueError):
                pass
        if self._wave_frames > 0:
            frames = max(1, self._wave_frames)
            elapsed = max(1e-6, time.monotonic() - self._wave_started_at)
            logger.info(
                "录音声纹结束：共 %d 帧，平均 渲染 %.2f ms / 合成 %.2f ms / 分层更新 %.2f ms"
                "（实测 %.1f fps）",
                self._wave_frames,
                self._wave_render_ms / frames,
                self._wave_compose_ms / frames,
                self._wave_update_ms / frames,
                frames / elapsed,
            )
            self.waveform_frame_count += self._wave_frames
        self._wave_frames = 0
        if reset_frame and not self._closing:
            self._frame_key = None
            self._apply_frame(force=True)

    def _wave_tick(self) -> None:
        """声纹单帧：读 level → 渲染 overlay → 合成到底座 → UpdateLayeredWindow。"""
        self._wave_job = None
        tick_start = time.perf_counter()
        if self._root is None:
            return
        if self._state != "recording" or not self._waveform_allowed():
            self._stop_wave(reset_frame=True)
            return
        try:
            base = self._ensure_wave_base(self._look)
            level = self._level
            t0 = time.perf_counter()
            overlay = waveform.render_overlay(
                level, self._size, state="recording", look=self._look,
                history=list(self._level_hist),
            )
            t1 = time.perf_counter()
            frame = waveform.compose(base, overlay)
            t2 = time.perf_counter()
            self._layer.update(frame, self._pos[0], self._pos[1])
            t3 = time.perf_counter()
        except Exception:  # noqa: BLE001 - 声纹失败回退麦克风帧，不能黑屏/崩溃
            logger.exception("录音声纹渲染失败，回退麦克风动画")
            self._wave_available = False
            self._stop_wave(reset_frame=True)
            return
        self._level_hist.append(level)
        self._wave_frames += 1
        self._wave_render_ms += (t1 - t0) * 1000.0
        self._wave_compose_ms += (t2 - t1) * 1000.0
        self._wave_update_ms += (t3 - t2) * 1000.0
        if self._wave_frames == 1:
            logger.info(
                "录音声纹首帧：渲染 %.2f ms + 合成 %.2f ms + 分层更新 %.2f ms"
                "（%dpx，level=%.3f）",
                (t1 - t0) * 1000.0, (t2 - t1) * 1000.0, (t3 - t2) * 1000.0,
                self._size, level,
            )
        elif self._wave_frames % WAVE_LOG_EVERY == 0:
            frames = self._wave_frames
            logger.info(
                "录音声纹 %d 帧平均：渲染 %.2f ms + 合成 %.2f ms + 分层更新 %.2f ms"
                "（level=%.3f）",
                frames, self._wave_render_ms / frames, self._wave_compose_ms / frames,
                self._wave_update_ms / frames, level,
            )
        try:
            # 自校正：把本帧渲染耗时从间隔里扣掉，命中 30fps 时平均周期仍是 33ms
            spent_ms = (time.perf_counter() - tick_start) * 1000.0
            delay_ms = max(1, int(round(WAVE_MS - spent_ms)))
            self._wave_job = self._root.after(delay_ms, self._wave_tick)
        except tk.TclError:
            self._wave_job = None

    # ------------------------------------------------------------------
    # 消息气泡
    # ------------------------------------------------------------------
    def _show_message(self, message: str, duration_ms: int | None = None,
                      multiline: bool = False) -> None:
        """显示气泡消息。multiline=True 时保留换行不截断（隐私说明等长文案用）。"""
        text = _multiline_message(message) if multiline else _short_message(message)
        if not text:
            return
        if not self._show_bubbles:  # 调试/截图：只拍球体，不弹气泡
            # 隐私：气泡里可能有识别文本预览，日志只记字数不记内容
            logger.debug("气泡已禁用（%s）：%d 字", ENV_NO_BUBBLE, len(text))
            return
        show_ms = BUBBLE_SHOW_MS if duration_ms is None else max(300, int(duration_ms))
        if self._bubble is None:
            bubble = tk.Toplevel(self._root)
            bubble.overrideredirect(True)
            try:
                bubble.attributes("-topmost", True)
            except tk.TclError:
                pass
            frame = tk.Frame(bubble, bg=BUBBLE_BG, padx=10, pady=5)
            frame.pack()
            label = tk.Label(
                frame, text=text, fg=BUBBLE_FG, bg=BUBBLE_BG,
                font=("Microsoft YaHei UI", 10), justify="left",
            )
            label.pack()
            self._bubble = bubble
            self._bubble_label = label
            bubble.update_idletasks()
            self._apply_noactivate(bubble.winfo_id())
        else:
            self._bubble_label.configure(text=text)
        self._cancel_bubble_jobs()
        self._bubble_fade_alpha = 0.96
        try:
            self._bubble.attributes("-alpha", self._bubble_fade_alpha)
        except tk.TclError:
            pass
        self._position_bubble()
        self._bubble.deiconify()
        self._bubble.lift()
        logger.debug("气泡显示：%d 字（停留 %dms）", len(text), show_ms)
        self._bubble_hide_job = self._root.after(show_ms, self._start_bubble_fade)

    def _position_bubble(self) -> None:
        if self._bubble is None or self._root is None:
            return
        self._bubble.update_idletasks()
        width = max(self._bubble.winfo_width(), self._bubble.winfo_reqwidth())
        height = max(self._bubble.winfo_height(), self._bubble.winfo_reqheight())
        ball_x, ball_y = self._pos
        screen_w = self._root.winfo_screenwidth()
        screen_h = self._root.winfo_screenheight()
        x = ball_x + self._size + 10
        if x + width > screen_w - 4:
            x = ball_x - width - 10
        x = max(0, min(x, screen_w - width - 2))
        y = ball_y + (self._size - height) // 2
        y = max(0, min(y, screen_h - height - 2))
        self._bubble.geometry(f"+{x}+{y}")

    def _start_bubble_fade(self) -> None:
        self._bubble_hide_job = None
        self._bubble_fade_job = self._root.after(BUBBLE_FADE_STEP_MS, self._fade_bubble_step)

    def _fade_bubble_step(self) -> None:
        self._bubble_fade_job = None
        if self._bubble is None:
            return
        self._bubble_fade_alpha -= 0.12
        if self._bubble_fade_alpha <= 0.05:
            # 淡出结束直接销毁气泡窗口，而不是只 withdraw：
            # v3 分层窗口下，2 秒一次的置顶刷新（SetWindowPos 带 SWP_SHOWWINDOW）会把
            # withdraw 的 owned 气泡窗口重新显示，alpha 停在 0.12，球旁就留下一张
            # 半透明"图片"残影（用户实机反馈）。销毁后下次消息重新创建，彻底消除残影。
            self._destroy_bubble()
            logger.debug("气泡淡出结束，已销毁")
            return
        try:
            self._bubble.attributes("-alpha", self._bubble_fade_alpha)
        except tk.TclError:
            pass
        self._bubble_fade_job = self._root.after(BUBBLE_FADE_STEP_MS, self._fade_bubble_step)

    def _destroy_bubble(self) -> None:
        """取消气泡相关定时器并销毁气泡窗口（下次消息会重新创建）。"""
        self._cancel_bubble_jobs()
        bubble = self._bubble
        self._bubble = None
        self._bubble_label = None
        self._bubble_fade_alpha = 0.96
        if bubble is not None:
            try:
                bubble.destroy()
            except tk.TclError:
                pass

    def _cancel_bubble_jobs(self) -> None:
        if self._root is None:
            return
        for job in (self._bubble_hide_job, self._bubble_fade_job):
            if job is not None:
                try:
                    self._root.after_cancel(job)
                except (tk.TclError, ValueError):
                    pass
        self._bubble_hide_job = None
        self._bubble_fade_job = None

    # ------------------------------------------------------------------
    # 鼠标交互
    # ------------------------------------------------------------------
    def _on_drag_start(self, event) -> None:
        logger.debug("鼠标按下：x_root=%s y_root=%s", event.x_root, event.y_root)
        self._press_point = (event.x_root, event.y_root)
        self._dragged = False
        self._drag_origin = (
            event.x_root, event.y_root,
            self._pos[0], self._pos[1],
        )

    def _on_drag_move(self, event) -> None:
        if self._drag_origin is None or self._press_point is None:
            return
        if not self._dragged:
            dx = abs(event.x_root - self._press_point[0])
            dy = abs(event.y_root - self._press_point[1])
            if dx < CLICK_MOVE_THRESHOLD_PX and dy < CLICK_MOVE_THRESHOLD_PX:
                return  # 还在单击容差内，先不挪窗口
            self._dragged = True
        start_x, start_y, win_x, win_y = self._drag_origin
        new_x = win_x + (event.x_root - start_x)
        new_y = win_y + (event.y_root - start_y)
        screen_w = self._root.winfo_screenwidth()
        screen_h = self._root.winfo_screenheight()
        new_x = max(0, min(new_x, screen_w - self._size))
        new_y = max(0, min(new_y, screen_h - self._size))
        self._root.geometry(f"+{new_x}+{new_y}")
        self._pos = (int(new_x), int(new_y))
        if self._use_layer:
            # 分层窗口拖到哪，UpdateLayeredWindow 的 pptDst 就跟到哪（保持同步）
            self._frame_key = None
            self._apply_frame(force=True)
        if self._bubble is not None and self._bubble.winfo_viewable():
            self._position_bubble()

    def _on_drag_end(self, _event) -> None:
        dragged = self._dragged
        logger.debug("鼠标松开：dragged=%s", dragged)
        self._drag_origin = None
        self._press_point = None
        self._dragged = False
        if dragged:
            self._remember_position()
            return
        # 原地单击 = 开始/停止录音（同一位置再点一下结束）
        now = time.monotonic()
        if now - self._last_click_at < CLICK_DEBOUNCE_S:
            return  # 双击的第二下忽略
        self._last_click_at = now
        logger.info("悬浮球单击：切换录音状态（当前 %s）", self._state)
        self._menu_toggle()

    def _remember_position(self) -> None:
        """把当前坐标写回内存中的 config；磁盘写入在退出时进行。"""
        if self._root is None:
            return
        ui_cfg = self._config.setdefault("ui", {})
        if not isinstance(ui_cfg, dict):
            ui_cfg = {}
            self._config["ui"] = ui_cfg
        ui_cfg["pos_x"] = int(self._pos[0])
        ui_cfg["pos_y"] = int(self._pos[1])

    def _on_right_click(self, event) -> None:
        if self._menu is None or self._root is None:
            return
        try:
            self._menu.entryconfigure(0, label=self._toggle_label())
            try:
                self._menu.tk_popup(event.x_root, event.y_root)
            finally:
                try:
                    self._menu.grab_release()
                except tk.TclError:
                    pass
        except tk.TclError:
            # 窗口已在关闭流程中，忽略这次点击
            logger.debug("窗口已关闭，忽略右键菜单请求")

    def _toggle_label(self) -> str:
        return "停止语音输入" if self._state == "recording" else "开始语音输入"

    # ------------------------------------------------------------------
    # 菜单动作：回调一律不在 tk 线程里长时间执行
    # ------------------------------------------------------------------
    def _menu_toggle(self) -> None:
        self._dispatch(self._on_toggle, "开始/停止语音输入")

    def _menu_reload(self) -> None:
        if self._on_reload_model is None:
            self.set_state("error", "未配置模型重载功能")
            return
        self._dispatch(self._on_reload_model, "重新加载模型")

    def _menu_open_data_dir(self) -> None:
        """右键「打开数据目录」：用资源管理器打开程序目录（config.json / logs 所在）。"""
        opener = getattr(os, "startfile", None)
        if opener is None:  # 非 Windows 平台没有 os.startfile（正常不会走到）
            self.set_state(self._state, "仅 Windows 支持打开数据目录")
            return
        try:
            opener(str(APP_DIR))
        except OSError:
            logger.exception("打开数据目录失败：%s", APP_DIR)
            self.set_state("error", "打开数据目录失败")
            return
        logger.info("已打开数据目录：%s", APP_DIR)

    def _menu_clear_logs(self) -> None:
        """右键「清空日志」：清空 logs/app.log（正确处理 RotatingFileHandler 句柄）并气泡提示。"""
        try:
            cleared = clear_app_log()
        except OSError:
            logger.exception("清空日志失败：%s", LOG_DIR)
            self.set_state("error", "清空日志失败")
            return
        logger.info("日志已清空（清理前 %d 字节）", cleared)
        self.set_state(self._state, "日志已清空")

    def _menu_toggle_waveform(self) -> None:
        """右键「录音声纹」勾选：立即生效并写入 config["ui"]["waveform"]（退出时落盘）。"""
        enabled = bool(self._wave_var.get()) if self._wave_var is not None else not self._waveform_enabled
        self._waveform_enabled = enabled
        ui_cfg = self._config.setdefault("ui", {})
        if not isinstance(ui_cfg, dict):
            ui_cfg = {}
            self._config["ui"] = ui_cfg
        ui_cfg["waveform"] = enabled  # 内存写入，退出时由 _save_config 落盘
        if enabled and self._state == "recording" and self._waveform_allowed():
            self._start_wave()
        elif not enabled and self._wave_job is not None:
            self._stop_wave(reset_frame=True)
        logger.info("录音声纹：%s", "开" if enabled else "关")

    def _menu_set_look(self, code: str) -> None:
        """右键「外观」菜单：立即切换悬浮球外观并写入 config["ui"]["look"]。"""
        self._set_look(code)

    def _set_look(self, code: str) -> None:
        code = _normalize_look(code)
        if code == self._look:
            return
        started = time.perf_counter()
        old = self._look
        self._look = code
        ui_cfg = self._config.setdefault("ui", {})
        if not isinstance(ui_cfg, dict):
            ui_cfg = {}
            self._config["ui"] = ui_cfg
        ui_cfg["look"] = code  # 内存写入，退出时由 _save_config 落盘
        if self._look_var is not None:
            self._look_var.set(code)
        try:
            self._ensure_look_frames(code)
        except Exception:  # noqa: BLE001 - 渲染失败时退回原外观，不能黑屏
            logger.exception("切换外观失败，回退到 %s", old)
            self._look = old
            if self._look_var is not None:
                self._look_var.set(old)
            return
        self._frame_key = None
        self._apply_frame(force=True)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        logger.info(
            "悬浮球外观已切换：%s（%s），%.0f ms，真透明分层=%s",
            code, LOOK_VARIANTS[code], elapsed_ms, self._use_layer,
        )
        if self._bubble is not None and self._bubble.winfo_viewable():
            self._position_bubble()

    def _menu_quit(self) -> None:
        self._queue.put(("menu_quit", "", ""))

    def _dispatch(self, callback, action_name: str) -> None:
        if callback is None:
            return

        def _run() -> None:
            try:
                callback()
            except Exception:
                logger.exception("%s回调执行失败", action_name)

        threading.Thread(target=_run, name="voiceime-ui-callback", daemon=True).start()

    # ------------------------------------------------------------------
    # 关闭
    # ------------------------------------------------------------------
    def _shutdown(self, notify_app: bool = False) -> None:
        if self._closing:
            return
        self._closing = True
        self._cancel_bubble_jobs()
        self._stop_wave(reset_frame=False)
        for job in (self._poll_job, self._raise_job, self._privacy_notice_job):
            if job is not None:
                try:
                    self._root.after_cancel(job)
                except (tk.TclError, ValueError):
                    pass
        self._poll_job = None
        self._raise_job = None
        self._privacy_notice_job = None
        if self._root is not None:
            self._remember_position()
            self._save_config()
        if self._bubble is not None:
            self._destroy_bubble()
        if self._layer is not None:
            try:
                self._layer.dispose()
            except Exception:  # noqa: BLE001
                logger.debug("释放分层窗口失败（忽略）", exc_info=True)
            self._layer = None
            self._use_layer = False
        root = self._root
        self._root = None
        if root is not None:
            try:
                root.destroy()
            except tk.TclError:
                pass
        if notify_app and self._on_quit is not None:
            try:
                self._on_quit()
            except Exception:
                logger.exception("退出回调执行失败")

    def _save_config(self) -> None:
        try:
            save_config(self._config)
        except Exception:
            logger.warning("保存窗口位置到配置文件失败", exc_info=True)
