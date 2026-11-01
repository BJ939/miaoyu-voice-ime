# -*- coding: utf-8 -*-
"""把 SenseVoice ONNX 模型准备到本项目的 models/ 目录（离线，不联网）。

妙语 MiaoYu 不附带模型（约 230 MB）。本脚本只做本地文件操作：

1. 从 ``--from`` 指定的已有模型目录查找模型；
2. 复制到项目 ``models/``：``model*.onnx`` + ``tokens.txt``（有 VAD 时再复制
   ``silero/silero_vad.onnx``）。

用法::

    python -B scripts/prepare_model.py --from D:\\models\\sensevoice-onnx   # 从指定目录复制
    python -B scripts/prepare_model.py --from ... --dry-run   # 只打印将要复制的文件
    python -B scripts/prepare_model.py --check     # 只检查 models/ 是否已就绪（退出码 0/1）
    python -B scripts/prepare_model.py --from ... --force     # 覆盖 models/ 里已存在的同名文件

没有指定 ``--from`` 或找不到模型时打印手动准备说明（含官方模型下载页）；
本脚本**不会**访问网络、不会编造下载地址。
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from voiceime.paths import (  # noqa: E402
    APP_DIR,
    SENSEVOICE_MODEL_NAMES,
    ModelMissingError,
    discover_models,
)

MODELS_DIR = APP_DIR / "models"
SILERO_DIR = MODELS_DIR / "silero"


def _fmt_size(num_bytes: int) -> str:
    return f"{num_bytes / (1024 * 1024):.1f} MB"


def _local_ready() -> tuple[bool, list[str]]:
    """检查 models/ 下是否已有一套可用模型。"""
    details: list[str] = []
    if not MODELS_DIR.is_dir():
        details.append(f"models/ 目录不存在：{MODELS_DIR}")
        return False, details
    model = None
    for name in SENSEVOICE_MODEL_NAMES:
        candidate = MODELS_DIR / name
        if candidate.is_file() and candidate.stat().st_size > 0:
            model = candidate
            break
    if model is None:
        details.append(f"models/ 下缺少 model*.onnx（候选：{', '.join(SENSEVOICE_MODEL_NAMES)}）")
    else:
        details.append(f"模型  OK  {model.name}  {_fmt_size(model.stat().st_size)}")
    tokens = MODELS_DIR / "tokens.txt"
    if tokens.is_file() and tokens.stat().st_size > 0:
        details.append(f"词表  OK  tokens.txt  {_fmt_size(tokens.stat().st_size)}")
    else:
        details.append("词表  MISSING  models/tokens.txt")
    vad = SILERO_DIR / "silero_vad.onnx"
    if not vad.is_file():
        vad = MODELS_DIR / "silero_vad.onnx"
    if vad.is_file() and vad.stat().st_size > 0:
        details.append(f"VAD   OK  {vad.relative_to(MODELS_DIR)}  {_fmt_size(vad.stat().st_size)}")
    else:
        details.append("VAD   可选缺失（建议补齐，否则底噪可能被识别成幻觉文本）")
    ok = (model is not None) and tokens.is_file() and tokens.stat().st_size > 0
    return ok, details


def _find_source(from_dir: str | None) -> dict | None:
    """返回 discover_models() 的解析结果（源目录）；未指定或找不到返回 None。"""
    if from_dir:
        candidates = [Path(from_dir).expanduser()]
    else:
        candidates = []
    for root in candidates:
        try:
            return discover_models({"model_dir": str(root)})
        except ModelMissingError:
            continue
    return None


def _plan(source: dict) -> list[tuple[Path, Path]]:
    model = Path(source["model"])
    plan: list[tuple[Path, Path]] = [
        (model, MODELS_DIR / model.name),
        (Path(source["tokens"]), MODELS_DIR / "tokens.txt"),
    ]
    vad = source.get("vad")
    if vad:
        plan.append((Path(vad), SILERO_DIR / "silero_vad.onnx"))
    return plan


def _print_manual_help() -> None:
    print("[FAIL] 未找到可复制的 SenseVoice 模型。")
    print("")
    print("准备方式（任选一种）：")
    print("  1) 手动下载 ONNX 模型并按下面的结构放好：")
    print("       models\\model.int8.onnx        （或 model.onnx / model.fp32.onnx）")
    print("       models\\tokens.txt")
    print("       models\\silero\\silero_vad.onnx （可选但强烈建议）")
    print("     下载页：https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models")
    print("     （推荐文件名：sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2）")
    print("  2) 本机已有模型目录时，用 --from 指定，让本脚本复制：")
    print("       python -B scripts\\prepare_model.py --from <已有的 sensevoice 模型目录>")
    print("  3) 或在 config.json 里设置 \"model_dir\": \"<已有的 sensevoice 模型目录>\"")
    print("")
    print("模型许可：SenseVoice 权重遵循 FunASR Model License v1.1（允许使用/修改/分享，但须署名来源）；")
    print("本项目不附带、不下载模型，也不访问网络。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="准备妙语 MiaoYu 的 SenseVoice 模型到 models/（离线）")
    parser.add_argument("--from", dest="from_dir", metavar="DIR",
                        help="从指定模型目录复制（不指定则打印手动准备说明）")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要执行的操作，不复制")
    parser.add_argument("--check", action="store_true", help="只检查 models/ 是否已就绪")
    parser.add_argument("--force", action="store_true", help="覆盖 models/ 里已存在的同名文件")
    args = parser.parse_args(argv)

    print(f"项目目录：{APP_DIR}")
    print(f"模型目录：{MODELS_DIR}")
    print("")

    if args.check:
        ok, details = _local_ready()
        for line in details:
            print(f"  {line}")
        print("")
        print("== models/ 就绪 ==" if ok else "== models/ 未就绪（运行本脚本或按提示手动放置）==")
        return 0 if ok else 1

    source = _find_source(args.from_dir)
    if source is None:
        if args.from_dir:
            print(f"[FAIL] 在指定目录下未找到可用的 SenseVoice 模型：{args.from_dir}")
            print("       需要 model*.onnx + tokens.txt（可选 silero/silero_vad.onnx）。")
            print("")
        else:
            print("[INFO] 未指定 --from（本脚本只做本地复制，不会联网下载模型）。")
            print("")
        _print_manual_help()
        return 1

    plan = _plan(source)
    print("找到模型来源：")
    for src, dst in plan:
        print(f"  {src}")
        print(f"    -> {dst.relative_to(APP_DIR)}  ({_fmt_size(src.stat().st_size)})")
    print("")

    if args.dry_run:
        print("[DRY-RUN] 未复制任何文件。去掉 --dry-run 即执行复制。")
        return 0

    started = time.perf_counter()
    copied = 0
    skipped = 0
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    for src, dst in plan:
        if not src.is_file():
            print(f"[FAIL] 源文件不存在：{src}")
            return 1
        if dst.exists() and not args.force:
            if dst.stat().st_size == src.stat().st_size:
                print(f"[SKIP] 已存在且大小一致：{dst.relative_to(APP_DIR)}")
                skipped += 1
                continue
            print(f"[FAIL] 目标已存在且大小不同：{dst.relative_to(APP_DIR)}（用 --force 覆盖）")
            return 1
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied += 1
        print(f"[COPY] {dst.relative_to(APP_DIR)}  ({_fmt_size(dst.stat().st_size)})")

    elapsed = time.perf_counter() - started
    print("")
    print(f"完成：复制 {copied} 个文件，跳过 {skipped} 个，耗时 {elapsed:.2f}s")
    ok, details = _local_ready()
    for line in details:
        print(f"  {line}")
    if not ok:
        print("== 复制后校验失败，请检查上面的 MISSING 项 ==")
        return 1
    print("== models/ 已就绪 ==")
    print("接下来可运行：python -B app.py --check")
    return 0


if __name__ == "__main__":
    sys.exit(main())
