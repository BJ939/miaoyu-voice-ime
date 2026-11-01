# -*- coding: utf-8 -*-
"""voice-ime 全局配置：默认值、加载、保存、ffmpeg 探测。"""
from __future__ import annotations

import copy
import json
import os
import shutil
import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    # 打包成 EXE 后：配置/日志放在 exe 同目录（onedir 模式下可写）。
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = APP_DIR / "config.json"
LOG_DIR = APP_DIR / "logs"


class ConfigError(Exception):
    """配置或运行时路径不可用。"""


def default_config() -> dict:
    return {
        "hotkey": "ctrl+alt+space",
        "cancel_key": "esc",
        "quit_hotkey": "ctrl+alt+q",
        "mic_device": None,
        "warm_mic": True,
        "warm_mic_idle_seconds": 120,
        "warm_mic_buffer_seconds": 180,
        "ffmpeg_path": None,
        "language": "auto",
        "use_itn": True,
        "num_threads": 2,
        "model_dir": None,
        "vad_model": None,
        "insert_mode": "paste",
        "restore_clipboard": True,
        "paste_delay_ms": 60,
        "restore_delay_ms": 350,
        "max_record_seconds": 60,
        "min_record_seconds": 0.4,
        "preload_model": True,
        "ui": {
            "always_on_top": True,
            "pos_x": None,
            "pos_y": None,
            "opacity": 0.92,
            "scale": 1.0,
            "look": "A",
            "waveform": True,
            # 冷启动开麦就绪提示音（很轻的「可以说话了」提示；关闭后只保留气泡视觉）
            "ready_chime": True,
            # 首次启动隐私说明气泡只弹一次；弹完置 true（退出时随 config.json 落盘）
            "privacy_notice_shown": False,
        },
        "log_level": "info",
        # 隐私：识别出的文字默认不写日志（日志只记时长与「文本长度=N 字」）；
        # 排错需要时可在 config.json 里临时改成 true，此时识别完成日志才记 文本=%r。
        "log_transcript": False,
    }


def should_show_privacy_notice(cfg: dict) -> bool:
    """首次启动隐私说明是否还要弹：ui.privacy_notice_shown 未写或为 false 时返回 True。"""
    ui_cfg = (cfg or {}).get("ui") if isinstance(cfg, dict) else None
    if not isinstance(ui_cfg, dict):
        return True
    return not bool(ui_cfg.get("privacy_notice_shown", False))


def mark_privacy_notice_shown(cfg: dict) -> None:
    """在内存配置里标记「隐私说明已展示」（退出时由 save_config 落盘，之后不再弹）。"""
    if not isinstance(cfg, dict):
        raise TypeError("cfg 必须是 dict")
    ui_cfg = cfg.get("ui")
    if not isinstance(ui_cfg, dict):
        ui_cfg = {}
        cfg["ui"] = ui_cfg
    ui_cfg["privacy_notice_shown"] = True


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: Path | None = None) -> dict:
    """读取 config.json；文件不存在返回默认值，损坏则抛 ConfigError。"""
    target = Path(path) if path else CONFIG_PATH
    if not target.exists():
        return default_config()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"配置文件无法解析：{target}（{exc}）") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"配置文件根节点必须是对象：{target}")
    return _deep_merge(default_config(), raw)


def save_config(cfg: dict, path: Path | None = None) -> None:
    """原子写入 config.json（UTF-8 无 BOM，缩进 2）。"""
    target = Path(path) if path else CONFIG_PATH
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, target)


def ffmpeg_path(cfg: dict) -> str:
    """返回可用的 ffmpeg 路径：config.json 的 ffmpeg_path > 程序目录内置 > PATH > 抛 ConfigError。

    voice-ime 用 ffmpeg 的 dshow 输入枚举麦克风、采集 16k/mono/s16 PCM，
    因此必须能找到 ffmpeg.exe。发布包把 LGPL 版 ffmpeg 放在 MiaoYu.exe 同级
    （或同级 ``bin/``），所以优先探测程序目录，做到「解压即用」不依赖用户 PATH；
    源码运行/未内置时回退 PATH，仍可在 config.json 里用 ``ffmpeg_path`` 显式指定。
    """
    configured = (cfg or {}).get("ffmpeg_path")
    if configured:
        path = Path(str(configured)).expanduser()
        if path.is_file():
            return str(path)
        raise ConfigError(
            f"config.json 中 ffmpeg_path 指向的文件不存在：{configured}。"
            "请改成实际的 ffmpeg.exe 路径，或删除该项改用程序目录/PATH 里的 ffmpeg。"
        )
    for candidate in (APP_DIR / "ffmpeg.exe", APP_DIR / "bin" / "ffmpeg.exe"):
        if candidate.is_file():
            return str(candidate)
    found = shutil.which("ffmpeg")
    if found:
        return found
    raise ConfigError(
        "未找到 ffmpeg。发布包应把 ffmpeg.exe 放在程序目录（与 MiaoYu.exe 同级）"
        "或同级 bin/ 下；从源码运行时请安装 ffmpeg 并加入 PATH，"
        "或在 config.json 的 ffmpeg_path 中指定 ffmpeg.exe 的绝对路径。"
    )
