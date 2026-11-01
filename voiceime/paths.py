# -*- coding: utf-8 -*-
"""定位 SenseVoice 模型文件（sherpa-onnx ONNX 模型 + tokens + 可选 Silero VAD）。

搜索优先级（``discover_models``）：
1. ``config.json`` 的 ``model_dir`` —— 显式指定，最高优先级；
2. 程序目录下的 ``models/`` —— 默认位置（推荐）。

模型目录里需要有 ``model.int8.onnx`` / ``model.onnx`` / ``model.fp32.onnx`` 之一与
``tokens.txt``；VAD 模型 ``silero_vad.onnx`` 建议放在同级 ``silero/`` 子目录（可选，
但没有 VAD 时环境底噪可能被识别成幻觉文本）。
"""
from __future__ import annotations

import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    # 打包成 EXE 后：模型目录与 exe 同级（onedir 模式下 _internal 之外）。
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent.parent

SENSEVOICE_MODEL_NAMES = ("model.int8.onnx", "model.onnx", "model.fp32.onnx", "model.int8.bin")


class ModelMissingError(Exception):
    """模型文件缺失或不可读。"""


def _app_model_roots() -> list[Path]:
    """程序目录下的 models/（默认位置；目录不存在时也返回，便于错误提示给出期望路径）。"""
    return [APP_DIR / "models"]


def _pick_model(directory: Path) -> Path | None:
    for name in SENSEVOICE_MODEL_NAMES:
        candidate = directory / name
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    return None


def _resolve_in(root: Path) -> dict | None:
    """在给定根目录下尝试解析 model/tokens/vad。"""
    model = None
    model_dir = None
    if root.is_dir():
        model = _pick_model(root)
        if model is not None:
            model_dir = root
        else:
            for sub in sorted(root.iterdir()):
                if sub.is_dir() and "sense" in sub.name.lower():
                    model = _pick_model(sub)
                    if model is not None:
                        model_dir = sub
                        break
    if model is None or model_dir is None:
        return None
    tokens = model_dir / "tokens.txt"
    if not tokens.is_file():
        alt = model_dir / "tokens" / "tokens.txt"
        tokens = alt if alt.is_file() else None
    if tokens is None:
        return None
    vad = None
    for candidate in (
        root / "silero" / "silero_vad.onnx",
        model_dir.parent / "silero" / "silero_vad.onnx",
        model_dir / "silero_vad.onnx",
    ):
        if candidate.is_file():
            vad = candidate
            break
    return {"model": str(model), "tokens": str(tokens), "vad": str(vad) if vad else None}


def _apply_vad_override(resolved: dict, cfg: dict) -> dict:
    """config.json 里显式给了 vad_model 时覆盖自动发现结果。"""
    configured_vad = cfg.get("vad_model")
    if configured_vad:
        vad = Path(str(configured_vad)).expanduser()
        if vad.is_file():
            resolved["vad"] = str(vad)
    return resolved


def discover_models(cfg: dict | None = None) -> dict:
    """返回 {"model","tokens","vad"}（vad 可能为 None）。缺失时抛 ModelMissingError。"""
    cfg = cfg or {}
    explicit_dir = cfg.get("model_dir")
    if explicit_dir:
        resolved = _resolve_in(Path(str(explicit_dir)).expanduser())
        if resolved is None:
            raise ModelMissingError(f"config.json 中 model_dir 下未找到 SenseVoice 模型：{explicit_dir}")
        return _apply_vad_override(resolved, cfg)

    searched: list[str] = []
    for root in _app_model_roots():
        searched.append(str(root))
        resolved = _resolve_in(root)
        if resolved is not None:
            return _apply_vad_override(resolved, cfg)

    hint = "；".join(searched) if searched else "（未找到任何候选目录）"
    raise ModelMissingError(
        "未找到本地 SenseVoice 模型文件。已搜索：" + hint +
        "。请把模型放到本项目的 models/ 目录（需要 model*.onnx + tokens.txt，"
        "VAD 模型 silero_vad.onnx 建议放在 models/silero/ 下），或运行 "
        "scripts/prepare_model.py --from <模型目录> 复制，或在 config.json 的 model_dir 指定模型目录。"
    )
