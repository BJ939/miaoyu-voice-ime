# -*- coding: utf-8 -*-
"""隐私改进 + 数据目录/清空日志 + 首次隐私说明回归测试（纯逻辑，不依赖麦克风/GUI 窗口）。

覆盖（公开前隐私改进）：
1) 配置默认值：顶层 ``log_transcript=False``、``ui.privacy_notice_shown=False``（老 config
   缺键时合并默认值后同样是 False）；
2) 日志脱敏：``format_transcript_log`` 默认只记「文本长度=N 字」不记内容，
   ``log_transcript=True`` 才记 ``文本=%r``；``asr_done`` 全链路（含 state 气泡日志）
   默认日志不含识别文本，且识别文本照常注入、气泡照常预览（隐私只影响日志）；
3) 首次启动隐私说明：``should_show_privacy_notice`` / ``mark_privacy_notice_shown``
   标记逻辑、落盘后不再弹；``_maybe_show_privacy_notice`` 只弹一次；文案覆盖
   音频不出本机 / 识别文字默认不写日志 / 日志目录；
4) 右键菜单：新增「打开数据目录」（``os.startfile``）与「清空日志」（气泡「日志已清空」）；
5) 清空日志：RotatingFileHandler 句柄打开状态下截断无 NUL 空洞、轮转备份一并清除；
6) 文件日志 handler 写入的是脱敏文案（格式化层实测）。

运行：python -B tests/test_privacy.py
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 测试里不打印被捕获异常的 traceback（logger.exception 无 handler 时会走 lastResort 到 stderr）
logging.getLogger("voiceime.ui").addHandler(logging.NullHandler())

import app as appmod  # noqa: E402
from voiceime import config as cfgmod  # noqa: E402
from voiceime import ui as ui_mod  # noqa: E402

FAILURES: list[str] = []
SKIPS: list[str] = []
CHECKS = 0

SECRET = "机密语音内容ABC"  # 识别文本样例：绝不能出现在默认日志里


def check(condition: bool, message: str) -> bool:
    global CHECKS
    CHECKS += 1
    if condition:
        print("    [OK] %s" % message)
        return True
    print("    [FAIL] %s" % message)
    FAILURES.append(message)
    return False


def banner(title: str) -> None:
    print("-" * 100)
    print(title)


class FakeUI:
    """记录 set_state 调用的假 UI（不需要 Tk 窗口）。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def set_state(self, state, message="", bubble_ms=None) -> None:
        self.calls.append((state, message, bubble_ms))

    def set_level(self, level) -> None:  # pragma: no cover - 本测试不进入推流
        pass

    def set_level_available(self, available) -> None:
        pass

    def quit(self) -> None:  # pragma: no cover
        pass


class CaptureHandler(logging.Handler):
    """把日志记录收集到内存，用于断言日志内容。"""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record.getMessage())

    def text(self) -> str:
        return "\n".join(self.records)


def run_config_checks() -> None:
    banner("[1] 配置默认值：log_transcript / ui.privacy_notice_shown")
    cfg = cfgmod.default_config()
    check(cfg.get("log_transcript") is False,
          "顶层默认 log_transcript=False（识别文字默认不写日志）")
    check(isinstance(cfg.get("ui"), dict) and cfg["ui"].get("privacy_notice_shown") is False,
          "ui.privacy_notice_shown 默认 False（首启弹一次隐私说明）")
    check("log_transcript" in cfg, "config.json 键名固定为 log_transcript")

    # 老用户的 config.json 没有新键：合并默认值后行为必须是「默认不记文本」
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text(json.dumps({"hotkey": "ctrl+alt+space", "ui": {"look": "A"}}),
                        encoding="utf-8")
        loaded = cfgmod.load_config(path)
    check(loaded.get("log_transcript") is False,
          "老 config.json 缺 log_transcript → 合并默认 False（升级后默认脱敏）")
    check(loaded.get("ui", {}).get("privacy_notice_shown") is False,
          "老 config.json 缺 privacy_notice_shown → 默认 False（升级后首启弹一次）")

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text(json.dumps({"log_transcript": True,
                                    "ui": {"privacy_notice_shown": True}}),
                        encoding="utf-8")
        loaded = cfgmod.load_config(path)
    check(loaded.get("log_transcript") is True,
          "config.json 显式 log_transcript=true 会保留（排错开关可用）")
    check(cfgmod.should_show_privacy_notice(loaded) is False,
          "privacy_notice_shown=true → 不再弹隐私说明")


def run_privacy_notice_flag_checks() -> None:
    banner("[2] 首次隐私说明：文案 / 标记逻辑 / 落盘")
    cfg = cfgmod.default_config()
    check(cfgmod.should_show_privacy_notice(cfg) is True, "默认配置需要弹一次隐私说明")
    check(cfgmod.should_show_privacy_notice({"ui": {}}) is True, "ui 段无该键 → 需要弹")
    check(cfgmod.should_show_privacy_notice({"ui": {"privacy_notice_shown": False}}) is True,
          "显式 false → 需要弹")
    check(cfgmod.should_show_privacy_notice({}) is True, "没有 ui 段也不崩溃（按需要弹处理）")
    check(cfgmod.should_show_privacy_notice({"ui": "bad"}) is True,
          "ui 段类型损坏时按需要弹处理")

    cfgmod.mark_privacy_notice_shown(cfg)
    check(cfg["ui"]["privacy_notice_shown"] is True,
          "mark 后 ui.privacy_notice_shown=true（内存配置）")
    check(cfgmod.should_show_privacy_notice(cfg) is False, "标记后不再弹")
    cfg2: dict = {}
    cfgmod.mark_privacy_notice_shown(cfg2)
    check(isinstance(cfg2.get("ui"), dict) and cfg2["ui"]["privacy_notice_shown"] is True,
          "无 ui 段时 mark 自动补 ui 段（不 crash）")

    # 落盘 → 重新读取 → 仍然不再弹
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        cfgmod.save_config(cfg, path)
        reloaded = cfgmod.load_config(path)
    check(reloaded["ui"]["privacy_notice_shown"] is True,
          "privacy_notice_shown 随 save_config 落盘并在下次启动读回")
    check(cfgmod.should_show_privacy_notice(reloaded) is False,
          "落盘后下次启动不再弹（只弹一次）")

    text = ui_mod.privacy_notice_text(Path("D:/demo/logs"))
    print("    隐私说明文案：\n%s" % "\n".join("      " + line for line in text.splitlines()))
    check("音频只在本机处理" in text and "不联网" in text,
          "文案含「音频只在本机处理，不联网」")
    check("识别文字默认不写日志" in text, "文案含「识别文字默认不写日志」")
    check("日志目录" in text and "demo" in text, "文案含日志目录（日志路径可见）")
    check(ui_mod.PRIVACY_NOTICE_MS >= 3000,
          "隐私说明气泡停留 %dms，足够读完" % ui_mod.PRIVACY_NOTICE_MS)
    lines = ui_mod._multiline_message("甲\n\n  乙  ").splitlines()
    check(lines == ["甲", "乙"], "多行提示保留换行、去掉空行且不截断")


def run_log_redaction_checks() -> None:
    banner("[3] 日志脱敏：format_transcript_log / asr_done 全链路")
    masked = appmod.format_transcript_log(2.50, 0.30, SECRET, False)
    print("    默认（log_transcript=False）：%s" % masked)
    check(("文本长度=%d 字" % len(SECRET)) in masked,
          "默认格式只记「文本长度=%d 字」" % len(SECRET))
    check(SECRET not in masked, "默认格式不含识别文本")
    check("文本=" not in masked, "默认格式不出现「文本=」字段")
    check(masked.startswith("识别完成：2.50s 音频 / 0.30s 推理"),
          "识别完成日志仍保留音频/推理耗时（排错能力保留）")

    shown = appmod.format_transcript_log(2.50, 0.30, SECRET, True)
    print("    排错（log_transcript=True）：%s" % shown)
    check(SECRET in shown and "文本=" in shown,
          "log_transcript=True 时记录 文本=%r（排错可用）")
    check(appmod.format_transcript_log(1.0, 0.1, "", False).endswith("文本长度=0 字"),
          "空文本 → 文本长度=0 字（无内容泄漏）")
    check(appmod.format_transcript_log(1.0, 0.1, None, False).endswith("文本长度=0 字"),
          "text=None 也不崩溃（文本长度=0 字）")
    check(appmod.format_transcript_log(1.0, 0.1, "  前后空格  ", False).endswith("文本长度=4 字"),
          "长度按去掉首尾空白后的字符数统计")

    # 全链路：asr_done 事件 → 日志（默认配置）
    def run_pipeline(cfg: dict) -> tuple[str, list, list]:
        capture = CaptureHandler()
        log = appmod.LOG
        old_level = log.level
        log.addHandler(capture)
        log.setLevel(logging.DEBUG)
        try:
            application = appmod.VoiceIME(cfg, with_ui=True)
            fake = FakeUI()
            application.set_ui(fake)
            injected: list[str] = []
            application._inject = lambda text: injected.append(text)
            application._toast_later = lambda *args, **kwargs: None
            application._handle_event(("asr_done", SECRET, 2.5, 0.3))
        finally:
            log.removeHandler(capture)
            log.setLevel(old_level)
        return capture.text(), injected, fake.calls

    log_text, injected, ui_calls = run_pipeline(cfgmod.default_config())
    print("    默认配置 asr_done 捕获日志：")
    for line in log_text.splitlines():
        print("      " + line)
    check(SECRET not in log_text, "asr_done 全链路默认日志不含识别文本（含 state 日志）")
    check(("文本长度=%d 字" % len(SECRET)) in log_text, "识别完成日志记录文本长度")
    check("已输入（识别文本不写日志）" in log_text, "「已输入」state 日志已脱敏")
    check(injected == [SECRET], "识别文本照常注入（隐私只影响日志，不影响功能）")
    bubble = [call[1] for call in ui_calls if call[0] == "idle"]
    check(any(msg == "已输入 " + SECRET for msg in bubble),
          "气泡仍显示识别文本预览（界面提示不受影响）")

    log_text2, _injected2, _ui2 = run_pipeline({**cfgmod.default_config(), "log_transcript": True})
    print("    log_transcript=True 捕获日志：")
    for line in log_text2.splitlines():
        print("      " + line)
    check(SECRET in log_text2 and "文本=" in log_text2,
          "log_transcript=True 时识别完成日志记录原文（排错模式）")
    check("已输入（识别文本不写日志）" in log_text2,
          "即使排错模式，气泡 state 日志仍不输出识别文本")


def run_menu_checks() -> None:
    banner("[4] 右键菜单：打开数据目录 / 清空日志")
    labels = [label for label, _method in ui_mod.EXTRA_MENU_COMMANDS]
    print("    隐私/数据菜单项：%r" % (labels,))
    check("打开数据目录" in labels, "菜单包含「打开数据目录」")
    check("清空日志" in labels, "菜单包含「清空日志」")
    for label, method_name in ui_mod.EXTRA_MENU_COMMANDS:
        check(callable(getattr(ui_mod.Indicator, method_name, None)),
              "「%s」回调 Indicator.%s 存在" % (label, method_name))

    indicator = ui_mod.Indicator(cfgmod.default_config())
    check(indicator._menu is None, "Indicator 初始化不需要 Tk 窗口（菜单在 run() 里创建）")

    # 「打开数据目录」→ os.startfile(程序目录)
    called: list[str] = []
    original_startfile = getattr(os, "startfile", None)
    try:
        os.startfile = lambda path: called.append(path)  # type: ignore[attr-defined]
        indicator._menu_open_data_dir()
    finally:
        if original_startfile is not None:
            os.startfile = original_startfile  # type: ignore[attr-defined]
        else:  # pragma: no cover - 非 Windows
            del os.startfile  # type: ignore[attr-defined]
    check(called == [str(ui_mod.APP_DIR)],
          "「打开数据目录」调用 os.startfile(程序目录=%s)" % ui_mod.APP_DIR)

    # 打开目录失败（如被安全软件拦截）不能崩
    failed: list[str] = []

    def _deny(_path: str) -> None:
        failed.append(_path)
        raise OSError("denied")

    try:
        os.startfile = _deny  # type: ignore[attr-defined]
        indicator._menu_open_data_dir()
    except Exception as exc:  # noqa: BLE001 - 失败必须被方法内部吞掉
        check(False, "「打开数据目录」失败时抛异常（%r）" % exc)
    finally:
        if original_startfile is not None:
            os.startfile = original_startfile  # type: ignore[attr-defined]
        else:  # pragma: no cover
            del os.startfile  # type: ignore[attr-defined]
    check(bool(failed), "「打开数据目录」失败被捕获（不崩 UI）")
    while True:  # 清掉失败路径留下的队列项，避免干扰下面的气泡断言
        try:
            indicator._queue.get_nowait()
        except Exception:  # noqa: BLE001 - queue.Empty
            break

    # 「清空日志」→ 清空 logs 目录里的 app.log（含轮转备份）+ 气泡「日志已清空」
    with tempfile.TemporaryDirectory() as tmp:
        log_dir = Path(tmp)
        (log_dir / "app.log").write_text("旧日志：识别文本 机密\n", encoding="utf-8")
        (log_dir / "app.log.1").write_text("旧轮转：识别文本 机密\n", encoding="utf-8")
        original_log_dir = ui_mod.LOG_DIR
        ui_mod.LOG_DIR = log_dir
        try:
            indicator._menu_clear_logs()
        finally:
            ui_mod.LOG_DIR = original_log_dir
        check((log_dir / "app.log").read_text(encoding="utf-8") == "",
              "「清空日志」后 app.log 为空")
        check(not (log_dir / "app.log.1").exists(), "轮转备份 app.log.1 一并清除")
        items: list[tuple] = []
        while True:
            try:
                items.append(indicator._queue.get_nowait())
            except Exception:  # noqa: BLE001 - queue.Empty
                break
        print("    气泡队列项：%r" % (items,))
        check(any(item[0] == "state" and item[2] == "日志已清空" for item in items),
              "「清空日志」后气泡提示「日志已清空」")


def run_clear_log_checks() -> None:
    banner("[5] 清空日志与 RotatingFileHandler 句柄（无 NUL 空洞）")
    with tempfile.TemporaryDirectory() as tmp:
        log_dir = Path(tmp)
        logger = logging.getLogger("voice-ime-privacy-test")
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        handler = logging.handlers.RotatingFileHandler(
            log_dir / "app.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        try:
            logger.info("旧日志 %s", SECRET)
            handler.flush()
            before = (log_dir / "app.log").stat().st_size
            cleared = ui_mod.clear_app_log(log_dir, logger=logger)
            check(cleared == before, "clear_app_log 返回清理前字节数（%d）" % before)
            check((log_dir / "app.log").read_text(encoding="utf-8") == "",
                  "清理后 app.log 为空")
            # handler 未重建：继续写日志不应在旧偏移留下 NUL 空洞
            logger.info("清空后的新日志")
            handler.flush()
            raw = (log_dir / "app.log").read_bytes()
            content = raw.decode("utf-8")
            print("    清理后文件 %d 字节：%r" % (len(raw), content))
            check(b"\x00" not in raw, "append 句柄继续写入无 NUL 空洞（流位置已归零）")
            check("清空后的新日志" in content, "清空后新日志正常写入")
            check(SECRET not in content, "旧识别文本已清除（新日志不含旧内容）")
            cleared_again = ui_mod.clear_app_log(log_dir, logger=logger)
            check(cleared_again > 0, "重复清空可用（返回新写入字节数 %d）" % cleared_again)
        finally:
            logger.removeHandler(handler)
            handler.close()
    with tempfile.TemporaryDirectory() as tmp:
        empty_dir = Path(tmp)
        cleared = ui_mod.clear_app_log(empty_dir)
        check(cleared == 0 and (empty_dir / "app.log").exists(),
              "日志文件不存在时创建空 app.log 并返回 0（不抛异常）")


def run_logging_setup_checks() -> None:
    banner("[6] 文件日志 handler：写入的是脱敏文案")
    with tempfile.TemporaryDirectory() as tmp:
        log_dir = Path(tmp)
        root = logging.getLogger()
        old_handlers = list(root.handlers)
        old_level = root.level
        original_log_dir = cfgmod.LOG_DIR
        cfgmod.LOG_DIR = log_dir
        try:
            appmod._setup_logging(cfgmod.default_config(), console=False)
            appmod.LOG.info(
                "%s", appmod.format_transcript_log(1.00, 0.10, SECRET, False)
            )
            for existing in list(root.handlers):
                existing.flush()
            content = (log_dir / "app.log").read_text(encoding="utf-8")
        finally:
            for existing in list(root.handlers):
                root.removeHandler(existing)
                existing.close()
            for existing in old_handlers:
                root.addHandler(existing)
            root.setLevel(old_level)
            cfgmod.LOG_DIR = original_log_dir
        print("    文件日志内容：%r" % content)
        check(("文本长度=%d 字" % len(SECRET)) in content,
              "文件日志写入「文本长度=N 字」")
        check(SECRET not in content, "文件日志不含识别文本")
        check("识别完成：1.00s 音频 / 0.10s 推理" in content, "耗时信息仍写入文件日志")


def run_privacy_notice_once_checks() -> None:
    banner("[7] 首次启动只弹一次（_maybe_show_privacy_notice）")
    cfg = cfgmod.default_config()
    indicator = ui_mod.Indicator(cfg)
    shown: list[tuple] = []
    indicator._show_message = lambda message, duration_ms=None, multiline=False: shown.append(
        (message, duration_ms, multiline)
    )
    indicator._maybe_show_privacy_notice()
    check(len(shown) == 1, "首次调用弹 1 次隐私说明气泡")
    message, duration_ms, multiline = shown[0]
    check(multiline is True, "隐私说明用多行气泡（不截断成「隐私说明（只显示一…」）")
    check(duration_ms == ui_mod.PRIVACY_NOTICE_MS,
          "停留时长 = PRIVACY_NOTICE_MS（%dms）" % ui_mod.PRIVACY_NOTICE_MS)
    check("音频只在本机处理" in message and "识别文字默认不写日志" in message,
          "气泡内容覆盖隐私三点")
    check(cfg["ui"]["privacy_notice_shown"] is True, "弹完写 ui.privacy_notice_shown=true")
    indicator._maybe_show_privacy_notice()
    check(len(shown) == 1, "再次启动不再弹（只弹一次）")
    check(indicator._privacy_notice_job is None, "弹完清理一次性定时任务引用")


def main() -> int:
    print("=" * 100)
    print("妙语 MiaoYu 隐私改进回归测试（日志默认不记识别文本 / 数据目录 / 清空日志 / 首次隐私说明）")
    print("=" * 100)
    run_config_checks()
    run_privacy_notice_flag_checks()
    run_log_redaction_checks()
    run_menu_checks()
    run_clear_log_checks()
    run_logging_setup_checks()
    run_privacy_notice_once_checks()
    print("=" * 100)
    print("检查项=%d，失败=%d，SKIP=%d" % (CHECKS, len(FAILURES), len(SKIPS)))
    for item in SKIPS:
        print("  SKIP: %s" % item)
    if FAILURES:
        print("结果：FAIL")
        for item in FAILURES:
            print("  - %s" % item)
        return 1
    print("结果：PASS（%d 项断言全部通过%s）" % (CHECKS, "；有 SKIP" if SKIPS else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
