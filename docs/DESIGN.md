# 妙语 (MiaoYu) 设计与接口契约

> 本文是妙语 (MiaoYu) 的设计文档，面向二次开发与贡献者：描述模块边界、公共接口、
> 关键实现约束与已知边界。使用说明见根目录 [README.md](../README.md)。

## 1. 概述

Windows 本地离线语音输入法「妙语 MiaoYu」：全局热键 / 鼠标单击悬浮球开关录音 → ffmpeg 采集
16k/mono/s16 PCM → sherpa-onnx（SenseVoice，INT8 ONNX）本地识别 → 剪贴板粘贴注入当前焦点窗口。
识别全程离线，不产生任何网络请求。界面为 Win32 分层窗口（逐像素 alpha）绘制的半透明悬浮球，
支持 A/E/F 三套外观与录音实时声纹。

## 2. 运行前提

- Windows 10/11 x64。dshow 采集、分层窗口、SendInput 均为 Win32 API，**不支持其他平台**。
- Python 3.10+（开发与验收使用 3.14.x）；tkinter 由官方 Python 安装包自带。
- 第三方包见 `requirements.txt`：Pillow、numpy、pynput、sherpa-onnx（>=2.0,<3）。
- ffmpeg 可执行（PATH 中，或 config.json 的 `ffmpeg_path` 指定）。源码运行需自行准备；
  发布包（Release 的完整包 / 精简包）已内置 LGPL 构建的 ffmpeg。
- SenseVoice ONNX 模型 + tokens；Silero VAD 模型（可选但强烈建议，见 README「模型准备」）。

## 3. 目录结构

```
app.py                 应用入口：VoiceIME 状态机、CLI、单实例与命名事件
voiceime/
  config.py            默认配置、config.json 读写、ffmpeg 探测
  paths.py             模型定位：config.model_dir > models/
  capture.py           常驻麦克风：ffmpeg dshow → stdout PCM → 环形缓冲 + LevelMeter
  audio.py             设备枚举 + 按需录音（临时 WAV，供常驻采集不可用时回退）
  asr.py               SenseVoice 识别：Silero VAD 分段 + 0.3s onset padding
  inject.py            剪贴板粘贴 / 逐字符 Unicode 注入
  hotkey.py            pynput 组合键监听（300ms 去抖）
  ui.py                悬浮球：Tk + 分层窗口、三外观、右键菜单、状态/气泡/声纹调度
  winlayer.py          Win32 分层窗口封装（UpdateLayeredWindow + 预乘 BGRA DIB）
  glass2.py            玻璃球预渲染（6 种方案；A/E/F 使用 light_frost/ghost/dark_frost）
  waveform.py          录音声纹 overlay 渲染与合成
  audio_level.py       声压级与自适应噪声门（LevelMeter）
build/
  build_exe.ps1        PyInstaller onedir 打包，产物同步到 Release/
  generate_icon.py     用 glass2.render_icon 生成 build/icon.ico（多尺寸）
tests/                 单元/集成/E2E 测试与 fixtures/*.wav
scripts/
  prepare_model.py     把本机已有模型复制到 models/（不联网）
  make_previews.py     渲染干净的 README 预览图（可选）
docs/                  本设计文档与产品截图
```

## 4. 状态机与数据流

所有事件在单一控制线程里串行处理，UI 只能通过线程安全方法投递：

```
idle --热键/单击--> recording --热键/单击/超时--> transcribing --成功--> 注入文本 --> idle
                        |--Esc--> idle
                        |--识别无语音--> idle（气泡提示“没听清”）
```

- 常驻麦克风路径（`warm_mic=true`，默认）：启动即后台打开 ffmpeg 采集，按热键只记录字节偏移、
  结束时从环形缓冲切片 → 消除“按热键到开麦”的 0.7~1.4s 延迟，也不会吞掉首个字。
  音频只在内存环形缓冲中处理，不落盘。空闲 `warm_mic_idle_seconds`（默认 120s）后释放麦克风，
  避免占用其他通话/录音程序。
  **冷启动过场**：空闲释放后重新开麦（及首次加载模型）属于冷启动，会先弹气泡
  「正在打开麦克风，请稍候…」；就绪后给「可以说话了」气泡（800ms）+ 很轻的提示音
  （880Hz/75ms，`ui.ready_chime` 可关），仅过渡耗时 >300ms 时触发；暖启动不弹过场、不响。
  打开失败时显式 error 红闪 + 「麦克风打开失败」气泡 + 原因 toast，不静默。
- 回退路径：常驻采集不可用（设备被占用/ffmpeg 异常）时，改用 `audio.AudioRecorder` 按需录音
  （写系统临时目录的 WAV，识别完成后立即删除）。
- 识别结果默认用剪贴板粘贴注入（`insert_mode=paste`），失败可切 `type` 逐字符输入。
- 日志：默认 `log_transcript=false`，识别文本只记长度、不记内容；右键菜单可「打开数据目录」
  与「清空日志」。首次运行展示一次隐私说明（`ui.privacy_notice_shown` 记忆）。

## 5. 模块接口契约

### 5.1 `voiceime/config.py`

```python
APP_DIR: pathlib.Path            # EXE 模式 = exe 所在目录；源码模式 = 项目根目录
CONFIG_PATH: pathlib.Path        # APP_DIR/config.json
LOG_DIR: pathlib.Path            # APP_DIR/logs

class ConfigError(Exception): ...
def default_config() -> dict
def load_config(path: Path | None = None) -> dict    # 不存在用默认值；深合并；非法 JSON 抛 ConfigError
def save_config(cfg: dict, path: Path | None = None) -> None   # 原子写，UTF-8 无 BOM
def ffmpeg_path(cfg: dict) -> str    # config.ffmpeg_path > PATH 的 ffmpeg > ConfigError（中文可读）
```

### 5.2 `voiceime/paths.py`

```python
class ModelMissingError(Exception): ...
def discover_models(cfg: dict | None = None) -> dict
# 返回 {"model": str, "tokens": str, "vad": str | None}；缺失抛 ModelMissingError
```

搜索顺序：`config.model_dir` → `APP_DIR/models`（默认位置）。
`models/` 下可直接放 `model*.onnx` + `tokens.txt`，也支持 `models/sensevoice-onnx/` 子目录；
VAD 模型从 `models/silero/silero_vad.onnx`（或模型同级 `silero/`）发现。

### 5.3 `voiceime/capture.py`

```python
class CaptureError(Exception): ...
class MicrophoneStream:
    def __init__(self, ffmpeg_path: str, device: str, sample_rate: int = 16000,
                 buffer_seconds: float = 180.0, audio_buffer_ms: int = 100): ...
    @property
    def warm(self) -> bool            # 采集进程存活且已收到音频
    def start(self, timeout: float = 10.0) -> None   # 等到 ≥200ms 音频才返回
    def close(self) -> None
    def mark(self) -> int             # 当前绝对字节偏移
    def available_seconds(self) -> float
    def slice_since(self, mark: int, flush_ms: int = 200) -> np.ndarray  # float32 [-1,1]
    # 声压级（UI 用；读线程写、UI 线程读）：level / raw_level / display_level
    meter: LevelMeter
```

### 5.4 `voiceime/audio.py`

```python
class AudioError(Exception): ...
def list_input_devices(ffmpeg_path: str) -> list[str]   # 解析 ffmpeg -list_devices 的 dshow 设备
def resolve_device(ffmpeg_path: str, configured: str | None) -> str
class AudioRecorder:
    def __init__(self, ffmpeg_path: str, device: str | None = None, sample_rate: int = 16000,
                 channels: int = 1, out_dir: str | None = None): ...
    def start(self) -> None
    def stop(self) -> str             # 写 stdin 'q' 优雅收尾，返回 16k/mono/s16 WAV 绝对路径
    def cancel(self) -> None
    @property
    def active(self) -> bool
    def elapsed(self) -> float
```

约束：`stop()` 后校验 WAV 可读且帧数 > 0；超时才 terminate/kill；异常一律 `AudioError`（中文）。
临时 WAV 由调用方（app.py）在识别完成后删除，取消路径立即删除。

### 5.5 `voiceime/asr.py`

```python
class AsrError(Exception): ...
@dataclass
class Transcript:
    text: str
    audio_seconds: float
    infer_seconds: float

class SpeechRecognizer:
    def __init__(self, model: str, tokens: str, vad_model: str | None = None,
                 language: str = "auto", use_itn: bool = True, num_threads: int = 2): ...
    def load(self) -> None            # 线程安全、幂等
    @property
    def loaded(self) -> bool
    def transcribe_wav(self, wav_path: str, language: str | None = None) -> Transcript
    def transcribe_samples(self, samples, sample_rate: int, language: str | None = None) -> Transcript
```

约束：
- `sherpa_onnx.OfflineRecognizer.from_sense_voice(...)`，样本 float32 [-1,1] 单声道；非 16k 输入线性重采样。
- VAD 模型可用时**每次识别都先过 Silero VAD**、只解码语音段（threshold=0.5 / min_silence=0.5 /
  min_speech=0.25 / max_speech=30 / window_size=512），首段起点前回补 0.3s，抑制底噪幻觉；
  无语音段返回 `text=""`。VAD 不可用时回退整段解码。
- 段落拼接：zh/yue/ja/ko/auto 用 `""`，en 用 `" "`；空音频/全静音不抛异常。
- 诊断属性：`last_used_vad` / `last_segment_count` / `last_vad_seconds`。

### 5.6 `voiceime/inject.py`

```python
class InjectionError(Exception): ...
def get_clipboard_text() -> str | None
def set_clipboard_text(text: str) -> bool
def insert_text(text: str, mode: str = "paste", restore_clipboard: bool = True,
                paste_delay_ms: int = 60, restore_delay_ms: int = 350) -> None
```

`paste`：备份文本剪贴板 → CF_UNICODETEXT 写入 → SendInput Ctrl+V → 延时 → 仅恢复文本型剪贴板
（原内容非文本时**不恢复**，避免破坏图片等）；`type`：KEYEVENTF_UNICODE 逐字符输入。

### 5.7 `voiceime/hotkey.py`

```python
class HotkeyError(Exception): ...
def parse_hotkey(spec: str) -> frozenset[str]
class HotkeyManager:
    def __init__(self, hotkey: str = "ctrl+alt+space", cancel_key: str = "esc",
                 quit_hotkey: str | None = "ctrl+alt+q",
                 on_toggle=None, on_cancel=None, on_quit=None): ...
    def start(self) -> None
    def stop(self) -> None
    @property
    def running(self) -> bool
```

用 `pynput.keyboard.Listener` 自行维护按下集合匹配组合键；回调在监听线程执行，只投递事件不得阻塞；
同一组合键 300ms 内只触发一次。

### 5.8 `voiceime/ui.py`

```python
LOOK_VARIANTS = {"A": "light_frost", "E": "ghost", "F": "dark_frost"}

class Indicator:
    def __init__(self, config: dict, on_toggle=None, on_quit=None, on_reload_model=None): ...
    def run(self) -> None                      # tkinter mainloop，主线程调用
    def quit(self) -> None                     # 线程安全
    def set_state(self, state: str, message: str = "") -> None   # idle|recording|transcribing|loading|error
    def set_level(self, level: float) -> None  # 录音声纹：只做一次 float 赋值
    def set_level_available(self, available: bool) -> None
    @property
    def waveform_active(self) -> bool
```

要求：无边框置顶悬浮球（56 逻辑 pt × `ui.scale`，DPI 感知后按物理像素渲染）；单击开始/结束录音
（移动 >4px 视为拖动；双击去抖 250ms）；不抢焦点（`WS_EX_NOACTIVATE`）；位置持久化到
`config["ui"]["pos_x"/"pos_y"]`；右键菜单：开始/停止、重新加载模型、录音声纹、打开数据目录、
清空日志、外观（A/E/F）、退出；短气泡 ≤12 汉字、2 秒淡出；每 2 秒重抬 topmost
（不带 `SWP_SHOWWINDOW`，避免把已销毁气泡重新显示）。

### 5.9 `voiceime/winlayer.py`

```python
class LayeredWindowError(RuntimeError): ...
def get_top_hwnd(child_hwnd: int) -> int
def is_layered(hwnd: int) -> bool
def premultiplied_bgra(image: Image.Image) -> bytes
class LayeredWindow:
    def __init__(self, hwnd: int): ...   # 置 WS_EX_LAYERED（失败抛 LayeredWindowError）
    def update(self, image: Image.Image, x: int, y: int) -> bool
    def invalidate(self) -> None
    @property
    def size(self) -> tuple[int, int] | None
    def dispose(self) -> None
```

实现：CreateDIBSection 32bpp top-down + 预乘 alpha BGRA + `UpdateLayeredWindow(ULW_ALPHA)`；
alpha=0 像素点击穿透，半透明像素可点击拖动。初始化/更新失败时 ui.py 自动回退色键方案。

### 5.10 `voiceime/glass2.py` / `waveform.py` / `audio_level.py`

```python
# glass2
VARIANTS = ("light_frost", "light_strong", "clear", "clear_ring", "ghost", "dark_frost")
FRAME_COUNTS = {"idle": 1, "recording": 12, "transcribing": 16, "loading": 16, "error": 8}
def render_variant(name, size=70, state="idle", phase=0.0) -> Image.Image   # RGBA
def render_frames(name, size=70, state="idle") -> list[Image.Image]
def render_icon(size=256) -> Image.Image
def render_base(name, size=70, state="recording") -> Image.Image   # 无麦克风 glyph 的底座

# waveform（v6 细线版）
BARS = 9                     # 竖线根数（奇数对称）
LINE_WIDTH_RATIO = 1/70      # 线宽（=静音线粗细）= size/70，70px → 1px
SPAN_RATIO = 0.66            # 9 根竖线横向总跨度
MAX_HEIGHT_RATIO = 0.80      # 竖线峰值 = 直径 80%（上下各留 10%）
LEVEL_GAMMA = 0.70
QUIET_LEVEL = 0.01           # level<1% 一律按静音画满直径横线（修浮点残值 bug）
def line_style(look) -> tuple[rgb, alpha]                 # 浅色玻璃深线 / 深色磨砂白线
def line_shadow(look) -> tuple[rgb, alpha, dx, dy] | None # 仅幽灵：极淡 1px 投影
def bar_x_positions(size_px, count=BARS) -> list[int]
def render_overlay(level, size_px, state="recording", look="A", history=None) -> Image.Image
def compose(base: Image.Image, overlay: Image.Image) -> Image.Image
def overlay_levels(level, count=9, seed=0) -> list[float]
def bar_heights(level, size_px, ...) -> list[float]

# audio_level
def level_from_rms(rms, floor_db=-60.0, ceil_db=-6.0) -> float
class LevelMeter:
    def __init__(self, sample_rate=16000, attack_ms=15.0, release_ms=180.0, ...): ...
    def feed(self, pcm_bytes: bytes) -> None
    def reset(self) -> None
    # 属性：level / display_level / raw_level / peak / rms / dbfs / noise_floor_db / gate_db
    def snapshot(self) -> dict
    def history(self, seconds: float = 0.6) -> list[float]
```

声纹设计（v6）：**纯细线**，不要圆角粗条/深色描边/外发光。
9 根竖直细线（线宽=静音线粗细=size/70，70px 下 1px；横向总跨度 0.66·size、居中对称、不出圆），
竖线峰值=直径 80%（上下各留 10%），按钟形 profile 从中心向两侧递减，条高对 level 做 `level^0.70`
gamma、并用 history 做错相；**静音 = 一条横跨整个圆直径的 1px 横细线**（目标分辨率直画，不超采样），
说话时不画横底线。线色按外观自适应：浅色玻璃 (24,28,36,α235)、深色磨砂白 (255,255,255,α250)、
幽灵白线 + 极淡 1px 投影 (10,14,22,α65)/(1,1)。`QUIET_LEVEL=0.01`：`display_level` 是指数衰减
浮点值（安静时仍为 1e-6~1e-3 正数），低于 1% 一律按静音画满直径横线（否则会出现 9 个 1px 小点、
满直径横线永不出现）。
`LevelMeter.level` 为固定 dBFS 映射（规格口径），`display_level` 带自适应噪声门（跟踪环境底噪、
门限=底噪+12dB），UI 使用后者，保证安静/嘈杂环境静音都显示为平线。

## 6. 配置项（`config.json`）

程序退出时写回；首次启动用默认值。完整键与默认值见 `voiceime/config.py::default_config()`：

| 键 | 默认 | 说明 |
|---|---|---|
| `hotkey` / `cancel_key` / `quit_hotkey` | `ctrl+alt+space` / `esc` / `ctrl+alt+q` | 切换录音 / 取消录音 / 退出 |
| `mic_device` | `null` | `null`=自动取第一个 dshow 输入设备；可填设备名 |
| `warm_mic` / `warm_mic_idle_seconds` / `warm_mic_buffer_seconds` | `true` / `120` / `180` | 常驻麦克风与环形缓冲 |
| `ffmpeg_path` | `null` | 指定 ffmpeg.exe；`null` 时用 PATH |
| `language` / `use_itn` / `num_threads` | `auto` / `true` / `2` | 识别参数 |
| `model_dir` / `vad_model` | `null` | 自定义模型目录 / VAD 路径 |
| `insert_mode` / `restore_clipboard` / `paste_delay_ms` / `restore_delay_ms` | `paste` / `true` / `60` / `350` | 注入方式与剪贴板恢复 |
| `max_record_seconds` / `min_record_seconds` | `60` / `0.4` | 单次录音上限 / 误触阈值 |
| `preload_model` | `true` | 启动即加载模型 |
| `log_transcript` | `false` | 日志是否记录识别文本（`false` 时只记长度，不写内容） |
| `ui.*` | — | `always_on_top`、`pos_x/pos_y`、`opacity`、`scale`、`look`(A/E/F)、`waveform`(默认 true)、`ready_chime`(默认 true，冷启动就绪提示音)、`privacy_notice_shown`(首次运行隐私说明是否已展示) |
| `log_level` | `info` | 日志级别（`logs/app.log`，滚动 1MB×3） |

## 7. 关键实现约束

- **单实例**：命名互斥体 `Local\voice-ime-single-instance`；重复启动返回码 3 并提示。
- **跨进程控制**：CLI `--toggle` / `--quit` / `--status` 通过命名事件
  `Local\voice-ime-toggle-event` / `Local\voice-ime-quit-event` 驱动已运行实例。
  注意这组名字在同机所有安装之间共享——**同一时间只应运行一个实例**，测试前先
  `python -B app.py --quit` 退出正在运行的实例。
- **真透明**：分层窗口逐像素 alpha；不使用 `-transparentcolor`、不叠加 Tk `-alpha`。
  窗口透明度由渲染 alpha 决定，`ui.opacity` 仅在色键回退模式生效。
- **计时精度**：录音声纹期间临时 `winmm.timeBeginPeriod(1)`（离开录音恢复），Tk `after(33)`
  配合自校正调度实测约 30fps；其余时间不影响系统定时器精度。
- **渲染性能**：70px 单帧声纹约 0.07~0.08ms、合成约 0.02ms、分层更新约 0.30~0.37ms（细线 v6 真机实测，30fps；旧描边粗条版约 0.6~1.06ms）。
- **日志与隐私**：日志写 `logs/app.log`（滚动 1MB×3）；默认不记录识别文本
  （`log_transcript=false` 时只写文本长度）；右键菜单支持清空日志；`logs/`、`config.json`、
  `models/` 均列入 `.gitignore`；程序不发起网络请求。

## 8. 已知边界与调试开关

- Windows 不提供单窗口「背后模糊」，磨砂/毛玻璃为预渲染视觉模拟；真透明本身是逐像素 alpha。
- 分层窗口不可用时自动回退旧色键方案（RGBA 合成到 `#0d1016` + alpha 阈值），三套外观仍可用；
  回退模式不支持录音声纹（保留麦克风动画）。
- UIPI：无法向以管理员权限运行的窗口注入文本；少数应用（密码框、部分游戏/远程桌面）不响应模拟
  Ctrl+V，可改用 `type` 模式。
- 悬浮球拖动范围基于主屏；多显示器/不同缩放组合下位置可能偏移。
- 纯 CPU 推理，模型常驻内存约 300MB。
- 调试用环境变量（不写入配置）：`VOICEIME_UI_LOOK=A|E|F` 强制初始外观；
  `VOICEIME_UI_FORCE_KEYED=1` 强制色键回退；`VOICEIME_UI_NO_BUBBLE=1` 关闭消息气泡（截图取证用）。

## 9. 测试矩阵

| 脚本 | 覆盖 | 环境要求 |
|---|---|---|
| `tests/test_asr.py` | 识别正确性 / VAD 分段 / 底噪幻觉回归 / RTF | 模型 |
| `tests/test_audio.py` | 真实麦克风 3s 录音 + WAV 合法性 | 麦克风（不可用 SKIP） |
| `tests/test_hotkey.py` | 组合键匹配 / 去抖 / 回调 | 桌面（无法模拟 SKIP） |
| `tests/test_inject.py` | 剪贴板往返 + 记事本粘贴读回 | 桌面焦点 |
| `tests/test_warm_capture.py` | 常驻采集切片（外放回环互相关） | 麦克风 + 扬声器（静音 SKIP） |
| `tests/test_app_flow.py` | 真 app 进程 + 热键事件 + 识别 + 注入 | 桌面 + 麦克风 |
| `tests/test_e2e.py` | 自检 + WAV 离线识别 | 模型 + ffmpeg |
| `tests/test_waveform.py` | 声压级包络 / 真实麦克风 / 细线渲染规格与几何 / 性能（60 项） | 麦克风（不可用 SKIP） |
| `tests/test_ui_click.py` | 悬浮球单击/双击去抖/拖动 | 桌面（单实例） |
| `tests/test_ui_waveform.py` | 录音声纹 UI 集成（截图像素断言，8 项；环境有声音时 SKIP） | 桌面 + 麦克风 |
| `tests/test_ready_signal.py` | 冷启动过场 / 就绪提示信号 / 设备名缓存（34 项） | 无（纯逻辑） |
| `tests/test_privacy.py` | 日志脱敏 / 隐私开关 / 菜单回调（纯逻辑） | 无 |
| `tests/make_fixtures.py` | 用 Windows SAPI 生成测试 WAV | Windows 中文/英文语音包 |

测试脚本用 `python -B tests/<script>.py` 运行（工作目录=项目根）；带 GUI 的测试会启动源码版窗口
抢焦点，且受全局单实例互斥体约束。

## 10. 变更记录（摘要）

- **v2**：悬浮球改为 PIL 预渲染毛玻璃位图（`voiceime/glass2.py`），状态用底部玻璃 LED；开启 DPI 感知。
- **v3**：真透明分层窗口（`voiceime/winlayer.py`）+ A/E/F 三套外观 + 右键「外观」切换与记忆；
  修复分层模式下消息气泡淡出后的残影窗口（置顶刷新去掉 `SWP_SHOWWINDOW`、气泡淡出后 destroy）。
- **v4**：录音实时声纹（`voiceime/audio_level.py` / `capture.py` / `waveform.py` / `ui.py` / `app.py`）：
  录音中麦克风图标消失，改为随声压波动的竖条，静音压成 1px 平线，约 30fps；右键可关。
- **v4.1**：声纹参数调整（最大条高 0.54·size、条宽 0.050·size、`level^0.70` gamma、静音线 1px 直画）。
- **v6**：录音声纹重做为**纯细线**——9 根 1px 竖线（线宽=size/70、无描边/发光/圆角粗条）、
  峰值=直径 80%、静音=横跨整个直径的横细线（说话时不画横底线）、按外观自适应线色（浅色玻璃深线/
  深色磨砂白线/幽灵白线+极淡投影）；`QUIET_LEVEL=0.01` 修复「静音横线永不出现」（display_level
  浮点残值被判为说话态）；真机实测 30fps、单帧渲染 0.08ms。
- **v6.1**：修复 windowed EXE 下 spawn ffmpeg 采集弹控制台窗口——`capture.py` 的
  `Popen` 补 `creationflags=CREATE_NO_WINDOW`（与 `audio.py` 一致）；真机验证启动首采 / 空闲释放后
  重开均 0 个新控制台窗口。同日记实测「空闲释放后首次录音」延迟 1.19s（ffmpeg dshow 打开设备
  645ms + 等满 200ms 首批音频 192ms + 设备枚举 129ms + 调度余量 94ms），为后续性能优化提供基线。
- **v6.2**：冷启动过场 + 就绪双信号（app.py/ui.py/config.py）：空闲释放后重新开麦先弹
  「正在打开麦克风，请稍候…」，就绪给「可以说话了」气泡（800ms）+ 轻提示音（winsound 内存 WAV，
  `ui.ready_chime` 可关，>300ms 才触发）；暖启动不弹不响；失败显式报错。同时两项无损性能优化：
  缓存麦克风设备名（免 `ffmpeg -list_devices` 129ms，失效自动重枚举重试一次）、首批音频等待
  200ms→120ms；冷启动 loading→recording 中位 830ms（原 1060ms，−21%）。新增 `tests/test_ready_signal.py`（34 项）。
- **v6.3（首个公开版本 v0.1.0，2026-11）**：更名「妙语 MiaoYu」，EXE 改名 `MiaoYu.exe`；
  隐私改进——`log_transcript` 默认 false（识别文本默认不写日志）、右键菜单新增「打开数据目录」
  「清空日志」、首次运行展示一次隐私说明；仓库整理为可公开结构（移除开发期临时文档，设计文档迁至
  `docs/DESIGN.md`）；模型定位收敛为 `model_dir` 与 `models/` 两处。
