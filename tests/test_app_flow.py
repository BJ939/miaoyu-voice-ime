# -*- coding: utf-8 -*-
"""整机端到端测试（Lead 维护）：真实 app 进程 + 热键事件 + 识别 + 注入。

分三段：
  A. 进程生命周期：启动 app.py --no-ui → --status/日志就绪 → --toggle 开始/结束 →
     静音录音应「没听清」且不注入 → --quit 干净退出。
  B. 确定性识别+注入：聚焦 WinForms 靶窗口，跑 `app.py --file zh_short.wav --insert`，
     断言靶窗口收到含「项目周报」的中文文本（真实识别 + 真实注入路径）。
  C. 外放回环（可选增强）：外放 TTS 后经热键录音，若能识别出语音则额外断言注入；
     小音量/麦克风收不到时打印 SKIP，不计失败（真实麦克风链路由 test_warm_capture 覆盖）。
运行：python -B tests/test_app_flow.py
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import test_inject as ti  # noqa: E402

from voiceime import config as cfgmod  # noqa: E402
from voiceime.inject import get_clipboard_text, set_clipboard_text  # noqa: E402

PY = sys.executable
APP = [PY, "-B", str(ROOT / "app.py")]
FIXTURE = TESTS / "fixtures" / "zh_short.wav"
CREATE_NO_WINDOW = 0x08000000


def run_app(*args: str, timeout: float = 90.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        APP + list(args),
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def read_log() -> str:
    path = cfgmod.LOG_DIR / "app.log"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def wait_log(pattern: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if re.search(pattern, read_log()):
            return True
        time.sleep(0.25)
    return False


def wait_status(timeout: float = 40.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if run_app("--status", timeout=20).returncode == 0:
                return True
        except subprocess.TimeoutExpired:
            pass
        time.sleep(0.3)
    return False


def amplify_wav(src: Path, gain: float) -> Path:
    with wave.open(str(src), "rb") as reader:
        params = reader.getparams()
        raw = reader.readframes(reader.getnframes())
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) * gain
    clipped = np.clip(samples, -32768, 32767).astype(np.int16)
    out = Path(tempfile.gettempdir()) / f"voiceime_flow_amp_{int(gain)}x.wav"
    with wave.open(str(out), "wb") as writer:
        writer.setparams(params)
        writer.writeframes(clipped.tobytes())
    return out


def launch_winforms_target():
    script = Path(tempfile.gettempdir()) / "voiceime_flow_target.ps1"
    script.write_text(ti.WINFORMS_SCRIPT, encoding="utf-8")
    shell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    proc = subprocess.Popen(
        [str(shell), "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", str(script)],
        creationflags=CREATE_NO_WINDOW,
    )
    found = ti.wait_for_window(lambda item: item[3] and item[2] == ti.WINFORMS_TITLE, timeout=25.0)
    if found is None:
        proc.kill()
        raise RuntimeError("WinForms 靶窗口未出现")
    hwnd = found[0]
    edits = [h for h, cls in ti.iter_children(hwnd) if "edit" in cls.lower()]
    if not edits:
        proc.kill()
        raise RuntimeError("WinForms 靶窗口没有 EDIT 子控件")
    return proc, hwnd, edits[0]


class Report:
    def __init__(self) -> None:
        self.fail = 0
        self.skip = 0

    def ok(self, name: str, detail: str = "") -> None:
        print(f"[PASS] {name}" + (f" | {detail}" if detail else ""))

    def bad(self, name: str, detail: str = "") -> None:
        print(f"[FAIL] {name}" + (f" | {detail}" if detail else ""))
        self.fail += 1

    def skipped(self, name: str, detail: str = "") -> None:
        print(f"[SKIP] {name}" + (f" | {detail}" if detail else ""))
        self.skip += 1


def wait_idle_after(log_from: int, timeout: float = 20.0) -> str:
    """等待 app 回到 idle（state -> idle 或 state -> error 之后）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        log = read_log()
        if re.search(r"state -> (idle|error)", log[log_from:]):
            return log
        time.sleep(0.2)
    return read_log()


def main() -> int:
    if not FIXTURE.exists():
        print(f"[SKIP] 缺少夹具 {FIXTURE}")
        return 0
    try:
        run_app("--quit", timeout=30)
    except subprocess.TimeoutExpired:
        pass
    time.sleep(1.0)

    cfgmod.LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = cfgmod.LOG_DIR / "app.log"
    log_path.write_text("", encoding="utf-8")

    report = Report()
    original_clipboard = get_clipboard_text()
    loud = amplify_wav(FIXTURE, 10.0)
    app_proc = None
    target_proc = None
    try:
        # ---------- A. 进程生命周期 + 静音不注入 ----------
        print("\n== A. app 进程生命周期与静音行为 ==")
        app_proc = subprocess.Popen(
            APP + ["--no-ui", "--console"],
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
        if wait_status(50):
            report.ok("app.py 启动并在 50s 内可被 --status 探测")
        else:
            report.bad("app.py 未能就绪（--status 不通）")
            print(read_log()[-3000:])
            return 1
        if wait_log("模型加载完成", 60):
            report.ok("模型加载完成")
        else:
            report.bad("模型未加载完成")
        if wait_log("麦克风常驻采集就绪", 30):
            report.ok("麦克风常驻采集就绪")
        else:
            report.skipped("麦克风常驻采集未就绪（将回退按需录音）")

        log_from = len(read_log())
        r = run_app("--toggle", timeout=30)
        if r.returncode == 0:
            report.ok("--toggle 开始录音事件送达")
        else:
            report.bad("--toggle 开始失败", r.stdout + r.stderr)
        if wait_log(r"state -> recording", 15):
            report.ok("进入 recording 状态")
        else:
            report.bad("未进入 recording 状态")
        time.sleep(2.0)  # 静音 2 秒
        run_app("--toggle", timeout=30)
        if wait_log(r"state -> (transcribing|idle|error)", 15):
            report.ok("结束录音触发识别/回到 idle")
        else:
            report.bad("结束录音后状态未推进")
        log = wait_idle_after(log_from, 20)
        if re.search(r"没听清|VAD 未检测到语音段|文本=''", log):
            report.ok("静音录音判定为「没听清」", "未注入幻觉文本")
        else:
            report.skipped("静音录音未被判空（环境噪声可能被当作语音）")
        if re.search(r"state -> idle 已输入", log):
            report.bad("静音录音不应注入文本")
        else:
            report.ok("静音录音未注入文本")

        # ---------- B. 确定性识别 + 注入 ----------
        print("\n== B. 确定性识别 + 注入（真实 app CLI 路径） ==")
        target_proc, hwnd, edit_hwnd = launch_winforms_target()
        print(f"[TARGET] hwnd={hwnd} edit={edit_hwnd}")
        if ti.force_foreground(hwnd, ti.control_center(edit_hwnd)):
            report.ok("靶窗口获得前台焦点")
        else:
            report.bad("靶窗口未获得前台焦点")
        ti.clear_focused_document()
        r = run_app("--file", str(FIXTURE), "--insert", timeout=120)
        print(r.stdout.strip())
        deadline = time.monotonic() + 15
        content = ""
        while time.monotonic() < deadline:
            content = ti.read_control_text(edit_hwnd)
            if content.strip():
                break
            time.sleep(0.2)
        if "项目周报" in content:
            report.ok("靶窗口收到含「项目周报」的注入文本", content)
        else:
            report.bad("靶窗口未收到预期文本", repr(content))

        # ---------- C. 外放回环（可选） ----------
        print("\n== C. 外放回环录音注入（可选增强） ==")
        import winsound

        ti.force_foreground(hwnd, ti.control_center(edit_hwnd))
        ti.clear_focused_document()
        run_app("--toggle", timeout=30)
        time.sleep(0.4)
        winsound.PlaySound(str(loud), winsound.SND_FILENAME)
        time.sleep(0.3)
        run_app("--toggle", timeout=30)
        deadline = time.monotonic() + 20
        loopback_content = ""
        while time.monotonic() < deadline:
            loopback_content = ti.read_control_text(edit_hwnd)
            if loopback_content.strip():
                break
            time.sleep(0.25)
        if re.search(r"VAD 未检测到语音段", read_log()[log_from:]):
            report.skipped("外放回环音量不足，VAD 未检测到语音（真实麦克风链路由 test_warm_capture 覆盖）")
        elif "项目周报" in loopback_content:
            report.ok("外放回环录音 → 识别 → 注入全链路", loopback_content)
        elif loopback_content.strip():
            report.skipped("外放回环识别文本不完整但注入成功", loopback_content)
        else:
            report.skipped("外放回环没有产生注入文本", "环境音量/麦克风增益不足")

        # ---------- 退出 ----------
        r = run_app("--quit", timeout=30)
        if r.returncode == 0:
            report.ok("--quit 事件送达")
        else:
            report.bad("--quit 失败", r.stdout + r.stderr)
        if app_proc.poll() is None:
            try:
                app_proc.wait(timeout=20)
                report.ok("app 进程自行退出", f"exit={app_proc.returncode}")
            except subprocess.TimeoutExpired:
                report.bad("app 进程 20s 未退出")
        else:
            report.ok("app 进程已退出", f"exit={app_proc.returncode}")
    finally:
        try:
            run_app("--quit", timeout=30)
        except subprocess.TimeoutExpired:
            pass
        if app_proc is not None and app_proc.poll() is None:
            try:
                app_proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                app_proc.kill()
        if target_proc is not None:
            ti.user32.PostMessageW(int(hwnd), ti.WM_CLOSE, 0, 0)
            try:
                target_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                target_proc.kill()
        if original_clipboard is not None:
            set_clipboard_text(original_clipboard)
        for path in (loud, Path(tempfile.gettempdir()) / "voiceime_flow_target.ps1"):
            try:
                path.unlink()
            except OSError:
                pass

    print(f"\n=== 结果：{'全部通过' if report.fail == 0 else f'{report.fail} 项失败'} "
          f"（SKIP={report.skip}） ===")
    return 0 if report.fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
