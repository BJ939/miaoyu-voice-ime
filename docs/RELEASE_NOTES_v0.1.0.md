# 妙语 MiaoYu v0.1.0 发布说明

首个公开版本。Windows 本地离线语音输入法：按热键/点悬浮球说话，文字自动进入当前窗口，
识别全程在本机完成，不联网、不上传音频。

## 下载

| 文件 | 内容 | 说明 |
|---|---|---|
| `MiaoYu-v0.1.0-win-x64-full.zip` | 程序 + ffmpeg(LGPL) + 内置 SenseVoice 模型（含 VAD）+ 第三方许可文件 | 推荐，解压即用（约 350 MB） |
| `MiaoYu-v0.1.0-win-x64-lite.zip` | 程序 + ffmpeg(LGPL)，不含模型 | 已有模型 / 自行管理模型时使用 |
| `SHA256SUMS.txt` | 全部发布文件的 SHA256 校验值 | 下载后校验用 |

> 两个发布文件的 SHA256 见 Release 页附件 `SHA256SUMS.txt`；下载后可用下面命令自行校验并与它对比：
>
> ```powershell
> Get-FileHash .\MiaoYu-v0.1.0-win-x64-full.zip -Algorithm SHA256
> Get-FileHash .\MiaoYu-v0.1.0-win-x64-lite.zip -Algorithm SHA256
> ```

## 主要功能

- 全局热键 `Ctrl+Alt+Space` / 鼠标单击悬浮球开始、结束录音；`Esc` 取消；`Ctrl+Alt+Q` 退出。
- 全本地离线识别：SenseVoice（INT8 ONNX，sherpa-onnx）+ Silero VAD，纯 CPU 推理，无网络请求。
- 常驻麦克风：启动即后台采集，按热键零开麦延迟；空闲 120 秒自动释放设备。
- 识别结果自动注入当前焦点窗口：默认剪贴板粘贴（可恢复原剪贴板），可切逐字符 Unicode 输入兜底。
- 真透明悬浮球：Win32 分层窗口逐像素 alpha，球外点击穿透，不抢焦点。
- 三套外观（A 苹果风毛玻璃 / E 极简幽灵 / F 深色磨砂），右键菜单实时切换并记忆。
- 录音实时声纹：9 根 1px 细线随声压起伏，静音时压成一条横线（约 30fps）。
- 冷启动过场提示：重新开麦先提示「正在打开麦克风，请稍候…」，就绪时气泡变「可以说话了」
  + 一声很轻的提示音（`ui.ready_chime` 可关）。
- 单实例运行、滚动日志、可选开机自启；PyInstaller onedir 打包，免 Python 分发。

## 隐私改进（本版重点）

- **日志默认不记录识别文本**：新增 `log_transcript`（默认 `false`），默认只记录文本长度；
  需要排错时可临时开启。
- 右键菜单新增「打开数据目录」「清空日志」。
- 首次运行展示一次性隐私说明：音频只在本机处理、不联网、识别文字默认不写日志。
- 音频默认只在内存环形缓冲中处理，不落盘；回退录音路径的临时 WAV 在识别后立即删除。

## 安装（30 秒）

1. 下载对应 zip；右键 zip → 属性 → 勾选「解除锁定」。
2. 解压到任意有写权限的目录（不要放 `C:\Program Files\`）。
3. 双击 `MiaoYu.exe`，允许 Windows 麦克风权限。
4. 按 `Ctrl+Alt+Space` 说话，再按一次结束，文字进入当前窗口。

精简包需先准备模型：把 SenseVoice ONNX 模型（`model.int8.onnx` + `tokens.txt`，
建议加 `silero\silero_vad.onnx`）放进 `models\`，或在 `config.json` 的 `model_dir` 指定目录；
源码目录下也可运行 `python -B scripts\prepare_model.py --from <已有模型目录>` 复制
（该脚本只做本地复制，不联网）。

## 已知限制

- 仅支持 Windows 10/11 x64（依赖 dshow、分层窗口与 SendInput）。
- Windows UIPI：无法向以管理员权限运行的窗口注入文本，需同样以管理员启动本程序。
- 少数应用（密码框、部分全屏游戏、远程桌面、某些终端）不响应模拟 `Ctrl+V`；
  可改 `insert_mode: "type"`，但安全输入框仍无效。
- 多显示器 / 混合 DPI 下悬浮球保存位置可能有偏移。
- 纯 CPU 推理，模型常驻内存约 300 MB。
- **EXE 未代码签名**：SmartScreen 可能提示「Windows 已保护你的电脑」，
  点「更多信息 → 仍要运行」即可；建议先校验 SHA256。

## 许可与第三方组件

- 项目代码与文档：[MIT](../LICENSE) © 2026 妙语 (MiaoYu) contributors。
- SenseVoice 模型权重：版权归 Alibaba / FunAudioLLM，
  [FunASR Model License v1.1](https://github.com/modelscope/FunASR/blob/main/MODEL_LICENSE)，
  分发/使用须署名；ONNX 版本由 [k2-fsa/sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) 转换发布。
- sherpa-onnx：Apache-2.0；Silero VAD：MIT；numpy：BSD-3-Clause；Pillow：MIT-CMU；pynput：LGPLv3。
- 发布包内置 ffmpeg **LGPL 构建**（不随 Git 仓库分发），许可与源码信息见包内
  `THIRD_PARTY_LICENSES` 与 `PROVENANCE`。
- 测试音频 `tests/fixtures/*.wav` 由 Windows SAPI 合成，仅用于测试。

## 校验

```powershell
Get-FileHash .\MiaoYu-v0.1.0-win-x64-full.zip -Algorithm SHA256
```

把结果与 Release 页的 `SHA256SUMS.txt` 对比。
