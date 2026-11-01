# 参与贡献 / Contributing

感谢你愿意改进妙语（MiaoYu）。提交前请先看下面几点。

## 开发环境

- Windows 10/11 x64；Python 3.10+（开发使用 3.14）
- 安装依赖：`python -m pip install -r requirements.txt`
- 需要 ffmpeg：装在 PATH，或在 `config.json` 里指定 `ffmpeg_path`
- 模型不随仓库分发：把 `model.int8.onnx` + `tokens.txt`（可选 `silero/silero_vad.onnx`）放到 `models/`，或设置 `model_dir`
- 自检：`python -B app.py --check` 应全部可用

## 提交前请跑测试

```powershell
python -B tests\test_ready_signal.py   # 冷启动/就绪信号/设备缓存（纯逻辑）
python -B tests\test_waveform.py       # 声纹/麦克风（无麦克风会 SKIP）
python -B tests\test_ui_click.py       # 改 UI 必跑（需要先退出已运行实例）
python -B tests\test_app_flow.py       # 整机流程（SKIP 允许）
```

说明：

- 麦克风/模型/桌面相关的测试在 CI 或不具备条件的机器上会 **SKIP**，不会假报 PASS。
- GUI 测试会抢鼠标焦点；跑之前请先 `python -B app.py --quit` 退出正在运行的实例。
- 单实例互斥体（`Local\voice-ime-*`）同机全局共享：源码版与 EXE 不能同时运行。

## 提交规范

1. 一个 PR 只做一件事，说明动机、行为变化与测试结果；
2. 不要提交：`models/`、`config.json`、`logs/`、`build/dist/`、`build/work/`、`Release/` 以及任何个人路径/识别文本；
3. 改 UI 请附上一张截图（只截悬浮球/气泡区域，不要带桌面隐私内容）；
4. 不引入需要联网的运行时依赖（本项目坚持「全本地、离线」）。

## 许可

- 本项目代码与文档采用 MIT（见 `LICENSE`）；
- 模型权重与第三方组件遵循各自许可（见 `THIRD_PARTY_LICENSES/` 与 `PROVENANCE.md`）；
- 提交代码即表示同意以 MIT 许可发布你的贡献。
