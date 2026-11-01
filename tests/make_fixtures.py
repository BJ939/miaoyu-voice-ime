# -*- coding: utf-8 -*-
"""生成测试音频夹具（Windows SAPI 语音合成 + ffmpeg 转 16k/mono/s16 WAV）。

用法::

    python -B tests/make_fixtures.py            # 缺什么生成什么，已存在则复用
    python -B tests/make_fixtures.py --force    # 全部重新生成

产物（tests/fixtures/，均为 16000 Hz / mono / 16-bit PCM）：
    zh_short.wav  “今天下午三点开个会，记得带上项目周报。”        ≈5 秒
    en_short.wav  “Hello, this is a voice input test.”            ≈3 秒
    zh_long.wav   8 句中文 TTS 分段拼接（段间 0.35s 静音），≥35 秒，覆盖 Silero VAD 分段路径

测试脚本（tests/test_asr.py）在夹具缺失时会自动调用 ``ensure_fixtures()`` 补齐。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
TARGET_RATE = 16000
TARGET_CHANNELS = 1
TARGET_SAMPWIDTH = 2
LONG_MIN_SECONDS = 35.0
LONG_GAP_SECONDS = 0.35
LONG_EXTEND_UNTIL_SECONDS = 36.0

ZH_SHORT_TEXT = "今天下午三点开个会，记得带上项目周报。"
EN_SHORT_TEXT = "Hello, this is a voice input test."

# 中文语音候选（按优先级尝试，实际存在哪个用哪个）
ZH_VOICES = (
    "Microsoft Huihui Desktop",
    "Microsoft Huihui",
    "Microsoft Kangkang",
    "Microsoft Yaoyao",
)
EN_VOICES = (
    "Microsoft Zira Desktop",
    "Microsoft Zira",
    "Microsoft David Desktop",
    "Microsoft David",
)

# 长音频文本：每句 4~6 秒，8 句约 40 秒；含“项目周报”“开会”等关键词便于断言
LONG_TEXTS = (
    "我们先来测试一下这段比较长的中文语音，看看本地的识别引擎能不能准确地分段处理。",
    "今天下午三点开个会，记得带上项目周报和上个月的销售数据。",
    "会议地点改到了三楼的大会议室，请大家提前十分钟到场。",
    "如果有同事需要远程参加，请把会议链接发到项目群里。",
    "另外提醒一下，周五之前要把测试报告整理好，发送给项目负责人。",
    "这段音频故意做得比较长，用来覆盖静音检测和分段识别的完整流程。",
    "识别完成之后，结果会按照顺序拼接成一段完整的中文文字。",
    "如果中间出现停顿，分段逻辑应该能够正确地把它们重新连起来。",
)


class FixtureError(Exception):
    """测试夹具生成失败。"""


# --------------------------------------------------------------------- 工具
def fixture_paths() -> dict[str, Path]:
    """返回三个夹具的绝对路径（不保证已存在）。"""
    return {
        "zh_short": FIXTURES_DIR / "zh_short.wav",
        "en_short": FIXTURES_DIR / "en_short.wav",
        "zh_long": FIXTURES_DIR / "zh_long.wav",
    }


def _ffmpeg_path() -> str:
    try:
        from voiceime.config import ffmpeg_path, load_config

        return ffmpeg_path(load_config())
    except Exception as exc:  # noqa: BLE001 - 统一转成夹具生成错误
        raise FixtureError(f"无法定位 ffmpeg（test 夹具生成需要 ffmpeg）：{exc}") from exc


def _decode_console(raw: bytes) -> str:
    if not raw:
        return ""
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _ps_quote(text: str) -> str:
    return "'" + str(text).replace("'", "''") + "'"


def _powershell() -> str:
    for name in ("pwsh", "powershell"):
        found = shutil.which(name)
        if found:
            return found
    raise FixtureError("未找到 PowerShell（pwsh/powershell），无法用 SAPI 合成测试音频。")


def _synthesize(items: list[tuple[Path, str]], voices: tuple[str, ...], workdir: Path) -> str:
    """用 SAPI 把一个或多个文本合成为 wav，返回实际使用的语音名。"""
    script_lines = [
        "$ErrorActionPreference = 'Stop'",
        "Add-Type -AssemblyName System.Speech",
        "$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer",
        "$voice = $null",
        "foreach ($name in @(" + ", ".join(_ps_quote(v) for v in voices) + ")) {",
        "  try { $synth.SelectVoice($name); $voice = $name; break } catch { }",
        "}",
        "if ($null -eq $voice) { [Console]::Error.WriteLine('未找到可用 SAPI 语音'); exit 3 }",
        "$synth.Rate = 0",
        "$items = @(",
    ]
    for path, text in items:
        script_lines.append(
            "  @{ Path = %s; Text = %s }" % (_ps_quote(str(path)), _ps_quote(text))
        )
    script_lines += [
        ")",
        "foreach ($it in $items) {",
        "  $synth.SetOutputToWaveFile($it.Path)",
        "  $synth.Speak($it.Text)",
        "}",
        "$synth.Dispose()",
        "Write-Output ('voice=' + $voice)",
    ]
    script_path = workdir / "sapi_synth.ps1"
    # PowerShell 5.1 需要 BOM 才能正确读取中文；7 同样兼容
    script_path.write_text("\r\n".join(script_lines) + "\r\n", encoding="utf-8-sig")
    cmd = [
        _powershell(),
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600)
    except subprocess.TimeoutExpired as exc:
        raise FixtureError("SAPI 语音合成超时（超过 10 分钟）。") from exc
    except OSError as exc:
        raise FixtureError(f"无法运行 PowerShell 进行 SAPI 语音合成：{exc}") from exc
    if proc.returncode != 0:
        detail = _decode_console(proc.stderr).strip() or _decode_console(proc.stdout).strip()
        raise FixtureError(
            f"SAPI 语音合成失败（退出码 {proc.returncode}）。"
            + (f"输出：{detail[-500:]}" if detail else "")
        )
    for path, _text in items:
        if not path.is_file() or path.stat().st_size <= 44:
            raise FixtureError(f"SAPI 未生成有效音频文件：{path}")
    output = _decode_console(proc.stdout)
    voice = ""
    for line in output.splitlines():
        if line.strip().startswith("voice="):
            voice = line.strip()[len("voice="):]
    return voice


def _convert_to_16k(src: Path, dst: Path, ffmpeg: str) -> None:
    """ffmpeg 转码为 16000 Hz / mono / pcm_s16le。"""
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(src),
        "-vn",
        "-ac",
        str(TARGET_CHANNELS),
        "-ar",
        str(TARGET_RATE),
        "-c:a",
        "pcm_s16le",
        str(dst),
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise FixtureError(f"ffmpeg 转码失败：{exc}") from exc
    if proc.returncode != 0:
        detail = _decode_console(proc.stderr).strip()
        raise FixtureError(f"ffmpeg 转码失败（退出码 {proc.returncode}）：{detail[-500:]}")


def _read_target_frames(path: Path) -> bytes:
    """读取 16k/mono/16bit WAV 的原始 PCM 帧，并校验格式。"""
    try:
        with wave.open(str(path), "rb") as wav:
            params = (wav.getframerate(), wav.getnchannels(), wav.getsampwidth())
            frames = wav.getnframes()
            data = wav.readframes(frames)
    except (wave.Error, EOFError, OSError) as exc:
        raise FixtureError(f"无法读取音频文件：{path}（{exc}）") from exc
    if params != (TARGET_RATE, TARGET_CHANNELS, TARGET_SAMPWIDTH):
        raise FixtureError(
            f"音频格式不符合预期（需要 16000Hz/mono/16bit）：{path} 实际为 {params}"
        )
    if frames <= 0 or not data:
        raise FixtureError(f"音频文件为空：{path}")
    return data


def _write_wav(path: Path, pcm: bytes) -> None:
    """原子写入 16k/mono/16bit WAV。"""
    tmp = path.with_name(path.name + ".tmp")
    try:
        with wave.open(str(tmp), "wb") as wav:
            wav.setnchannels(TARGET_CHANNELS)
            wav.setsampwidth(TARGET_SAMPWIDTH)
            wav.setframerate(TARGET_RATE)
            wav.writeframes(pcm)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _frames_of(path: Path) -> int:
    with wave.open(str(path), "rb") as wav:
        return wav.getnframes()


def _duration(path: Path) -> float:
    return _frames_of(path) / float(TARGET_RATE)


def fixture_ok(path: Path, min_seconds: float) -> bool:
    """夹具存在且格式/时长符合要求。"""
    try:
        if not path.is_file():
            return False
        with wave.open(str(path), "rb") as wav:
            ok = (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (
                TARGET_RATE,
                TARGET_CHANNELS,
                TARGET_SAMPWIDTH,
            )
            frames = wav.getnframes()
        return ok and frames > 0 and frames / float(TARGET_RATE) >= min_seconds
    except (wave.Error, EOFError, OSError):
        return False


def _make_short(dst: Path, text: str, voices: tuple[str, ...], workdir: Path, ffmpeg: str) -> str:
    raw = workdir / (dst.stem + "_raw.wav")
    voice = _synthesize([(raw, text)], voices, workdir)
    converted = workdir / (dst.stem + "_16k.wav")
    _convert_to_16k(raw, converted, ffmpeg)
    data = _read_target_frames(converted)
    _write_wav(dst, data)
    return voice


def _make_long(dst: Path, workdir: Path, ffmpeg: str) -> str:
    """中文多句 TTS 拼接长音频；不足 36 秒则循环追加，保证 ≥35 秒覆盖 VAD 分段。"""
    raw_paths = [workdir / ("long_raw_%02d.wav" % i) for i in range(len(LONG_TEXTS))]
    items = list(zip(raw_paths, LONG_TEXTS))
    voice = _synthesize(items, ZH_VOICES, workdir)
    chunks: list[bytes] = []
    for raw in raw_paths:
        converted = raw.with_name(raw.stem + "_16k.wav")
        _convert_to_16k(raw, converted, ffmpeg)
        chunks.append(_read_target_frames(converted))

    gap = b"\x00" * (int(round(LONG_GAP_SECONDS * TARGET_RATE)) * TARGET_SAMPWIDTH)
    parts: list[bytes] = [gap]
    for chunk in chunks:
        parts.append(chunk)
        parts.append(gap)
    pcm = b"".join(parts)
    duration = len(pcm) / (TARGET_RATE * TARGET_SAMPWIDTH)
    rounds = 1
    while duration < LONG_EXTEND_UNTIL_SECONDS:
        extra: list[bytes] = []
        for chunk in chunks:
            extra.append(chunk)
            extra.append(gap)
        pcm += b"".join(extra)
        duration = len(pcm) / (TARGET_RATE * TARGET_SAMPWIDTH)
        rounds += 1
        if rounds > 6:  # 防御：正常 1 轮即可达标
            break
    _write_wav(dst, pcm)
    if duration < LONG_MIN_SECONDS:
        raise FixtureError(
            f"生成长音频时长不足：{duration:.2f}s（要求 ≥{LONG_MIN_SECONDS:.0f}s），"
            "请检查 SAPI 语音是否可用。"
        )
    print(
        "        zh_long: %d 句 x %d 轮拼接，%.2f 秒，%d 帧" % (len(chunks), rounds, duration, len(pcm) // 2)
    )
    return voice


# --------------------------------------------------------------------- 入口
def ensure_fixtures(force: bool = False, verbose: bool = True) -> dict[str, Path]:
    """确保三个夹具存在且合法；缺失或时长不足时才生成。返回名称 -> 绝对路径。"""
    paths = fixture_paths()
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    requirements = {"zh_short": 1.0, "en_short": 1.0, "zh_long": LONG_MIN_SECONDS}
    missing = [name for name, minimum in requirements.items() if force or not fixture_ok(paths[name], minimum)]
    if not missing:
        if verbose:
            for name in ("zh_short", "en_short", "zh_long"):
                print(
                    "[FIXTURES] 复用 %-9s %6.2fs  %s"
                    % (paths[name].name, _duration(paths[name]), paths[name])
                )
        return paths

    if verbose:
        print("[FIXTURES] 需要生成：%s（SAPI 语音合成 + ffmpeg 转码，首次约需 20~40 秒）" % ", ".join(missing))
    ffmpeg = _ffmpeg_path()
    workdir = Path(tempfile.mkdtemp(prefix="voiceime_fixtures_"))
    try:
        if "zh_short" in missing:
            voice = _make_short(paths["zh_short"], ZH_SHORT_TEXT, ZH_VOICES, workdir, ffmpeg)
            if verbose:
                print("[FIXTURES] 生成 zh_short.wav 成功（SAPI 语音：%s，%.2fs）" % (voice, _duration(paths["zh_short"])))
        if "en_short" in missing:
            voice = _make_short(paths["en_short"], EN_SHORT_TEXT, EN_VOICES, workdir, ffmpeg)
            if verbose:
                print("[FIXTURES] 生成 en_short.wav 成功（SAPI 语音：%s，%.2fs）" % (voice, _duration(paths["en_short"])))
        if "zh_long" in missing:
            voice = _make_long(paths["zh_long"], workdir, ffmpeg)
            if verbose:
                print("[FIXTURES] 生成 zh_long.wav 成功（SAPI 语音：%s，%.2fs）" % (voice, _duration(paths["zh_long"])))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    for name, minimum in requirements.items():
        if not fixture_ok(paths[name], minimum):
            raise FixtureError(
                f"夹具校验失败：{paths[name]}（需要 16000Hz/mono/16bit 且时长 ≥{minimum:.0f}s）"
            )
    return paths


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = list(argv if argv is not None else sys.argv[1:])
    force = "--force" in args or "-f" in args
    unknown = [a for a in args if a not in ("--force", "-f")]
    if unknown:
        print("用法：python -B tests/make_fixtures.py [--force]")
        return 2
    try:
        paths = ensure_fixtures(force=force, verbose=True)
    except FixtureError as exc:
        print("[ERROR] %s" % exc)
        return 1
    total = 0
    print("[FIXTURES] 完成，产物：")
    for name in ("zh_short", "en_short", "zh_long"):
        path = paths[name]
        frames = _frames_of(path)
        total += path.stat().st_size
        print(
            "  %-9s %8d 帧  %6.2fs  %8d 字节  %s"
            % (path.name, frames, frames / float(TARGET_RATE), path.stat().st_size, path)
        )
    print("[FIXTURES] 合计 %.2f MB" % (total / 1024.0 / 1024.0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
