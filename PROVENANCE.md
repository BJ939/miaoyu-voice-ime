# PROVENANCE — 妙语 MiaoYu（voice-ime）v0.1.0 发布物来源与完整性记录

本文件记录 **v0.1.0 发布包**中所有随包分发资产的来源、版本、许可与 **SHA-256 校验值**，
用于下游用户/审计方独立复核。

- 生成时间：**2026-11-01（Asia/Shanghai）**
- 目标平台：Windows 10/11 x64
- 发行渠道：GitHub 公开仓库 `miaoyu-voice-ime`，首版 `v0.1.0`，提供 `full` / `lite` 两个 zip
- 生成方式：模型与 ffmpeg 校验值均由**本机文件实算**（PowerShell `Get-FileHash -Algorithm SHA256`）；
  上游版本/大小/摘要来自 GitHub API 实测；许可全文见 `THIRD_PARTY_LICENSES/`
- 打包脚本：`build/package_release.ps1`（参数化版本，产出 `MiaoYu-v<ver>-win-x64-full.zip` /
  `MiaoYu-v<ver>-win-x64-lite.zip` 与 `SHA256SUMS.txt`）

> 说明：`models/`、`build/dist/`、`Release/`、ffmpeg 二进制均**不进入 Git 仓库**（见 `.gitignore`）；
> 它们只随发布 zip 分发。本文件的哈希是"发布包内实际文件"的基准值。

---

## 一、语音识别模型：SenseVoice（ONNX int8）

| 项目 | 值 |
| --- | --- |
| 模型包名（上游发布名） | `sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17` |
| 下载来源 | <https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models>（资产 `sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2`） |
| ONNX 转换/发布方 | **k2-fsa/sherpa-onnx**（<https://github.com/k2-fsa/sherpa-onnx>） |
| 上游权重来源 | FunAudioLLM/SenseVoice（<https://github.com/FunAudioLLM/SenseVoice>），ModelScope `iic/SenseVoiceSmall` |
| 许可 | **FunASR Model License v1.1**（版权归 Alibaba / FunAudioLLM；须署名）——全文见 `THIRD_PARTY_LICENSES/FunASR-Model-License-1.1.txt`；上游 ModelScope 卡片同时标注 Apache-2.0，本包按更严格者执行，见 `THIRD_PARTY_LICENSES/README.md` 第二节 |
| VAD 模型来源 | 同上 asr-models 页面的 `silero_vad.onnx`（Silero VAD，MIT） |

### 随包文件（SHA-256 为本机实算，2026-11-01）

| 包内路径 | 字节 | SHA-256 |
| --- | ---: | --- |
| `models/model.int8.onnx` | 239233841 | `c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51` |
| `models/tokens.txt` | 315894 | `f449eb28dc567533d7fa59be34e2abca8784f771850c78a47fb731a31429a1dc` |
| `models/silero/silero_vad.onnx` | 1807522 | `a35ebf52fd3ce5f1469b2a36158dba761bc47b973ea3382b3186ca15b1f5af28` |

模型文件合计 **241,357,257 字节（约 230.2 MiB）**。
`lite` 包**不含**上述模型文件（用户自备），`full` 包含。

---

## 二、FFmpeg（LGPL 构建）

| 项目 | 值 |
| --- | --- |
| 构建来源 | **BtbN/FFmpeg-Builds**（<https://github.com/BtbN/FFmpeg-Builds>） |
| Release tag（不可变） | `autobuild-2026-10-31-12-59` |
| 资产 | `ffmpeg-n8.1.3-27-g8dca1586f4-win64-lgpl-shared-8.1.zip` |
| 资产字节 / SHA-256 | 80356594 / `5cef7bc48e513f3cee7c881e8b2999d835541edb1846c3e89443a67f8f8556ab`（GitHub API `digest`，本机下载后实算一致） |
| 版本 | `n8.1.3-27-g8dca1586f4-20261031`（built with gcc 16.2.0，构建日期 2026-10-31） |
| 对应源码 commit | `8dca1586f468bfcbb78ae3ed618af1361d9f140a`（<https://github.com/FFmpeg/FFmpeg/archive/8dca1586f468bfcbb78ae3ed618af1361d9f140a.tar.gz>） |
| 许可 | **LGPL v3**（`--enable-version3`；**无** `--enable-gpl` / `--enable-nonfree`；`--disable-libx264 --disable-libx265 --disable-libxavs2 --disable-libxvid`）。来源/源码/非 GPL 证据详见 `THIRD_PARTY_LICENSES/FFmpeg-来源与源码说明.md` |
| 随包文件（`bin/`） | 见下表（**不含** `ffplay.exe`；不经修改，直接取自上述 zip 的 `bin/`） |

| 包内路径 | 字节 | SHA-256 |
| --- | ---: | --- |
| `bin/ffmpeg.exe` | 552960 | `9f3683fc0480bd4b54d42ae44702f6ac6320b3d4a12a36f3a70bfe37d02fd861` |
| `bin/ffprobe.exe` | 265216 | `d93588787cb39d4ea2e74cf9b88998a95d2706add6c1de94a3b021e588cd6bea` |
| `bin/avcodec-62.dll` | 91620864 | `71ad4d3a1b158d88b4c00e8c2e1eded880e9a8a791a3bf9ba65fd053120470d9` |
| `bin/avdevice-62.dll` | 5008384 | `d5597695134cd684b33229fa62485e8a5b504c30243d8a6c7000e62ec18fb5be` |
| `bin/avfilter-11.dll` | 31893504 | `3049fb1350d4c851f4fd844b2afc913427eba1b936fe3887d74ad6e11b31fe1f` |
| `bin/avformat-62.dll` | 19846144 | `56bbe329fb585b673cebf3d8859a963ec26f1057afd27fbf07b8871d59600001` |
| `bin/avutil-60.dll` | 3021824 | `46ad06470515bf0f9a0f8f560470c725002c22decc6d7c2ae7ea836b5d941950` |
| `bin/swresample-6.dll` | 747008 | `790d6feb953be3118560afe6f5faafc1e254f947229699f40f95d70034eecf62` |
| `bin/swscale-9.dll` | 12972544 | `2cc756f813f2ec5ebd6f67f7c019dfb5ff66b015cbdf0c1c313fec19feb93507` |

FFmpeg 随包文件合计 **165,928,448 字节（约 158.2 MiB）**。

> 注：本仓库 `build/dist/` 中旧预构建产物曾包含 **GPL** 版 FFmpeg
> （`ffmpeg version N-126856-ged27b2c498-20260925`，`--enable-gpl --enable-version3`）。
> v0.1.0 发布包由 `build/package_release.ps1` 在组装时**整体替换 `bin/` 目录**为上述 LGPL 构建，
> 并带自动校验：若 `bin/ffmpeg.exe -version` 的 configuration 含 `--enable-gpl` 则打包失败。

---

## 三、Python 运行时与依赖

| 项目 | 值 |
| --- | --- |
| Python | CPython 3.14.3（PSF-2.0） |
| 冻结工具 | PyInstaller 6.22.3（bootloader：GPLv2-or-later + 允许分发非自由程序的特殊例外） |
| 直接依赖 | Pillow 12.2.0（MIT-CMU）、numpy 2.4.4（BSD-3-Clause 等）、pynput 1.8.1（LGPLv3）、sherpa-onnx 2.0.0（Apache-2.0） |
| 传递依赖 | psutil 7.2.2、pywin32 311、PyYAML 6.0.3、click 8.5.0、charset-normalizer 3.4.7 |
| 详情与许可全文 | `THIRD_PARTY_LICENSES/Python-deps.txt`、`THIRD_PARTY_LICENSES/python/` |

版本采集方式：本机 `importlib.metadata` 实测，并与 `build/dist/MiaoYu/_internal/*.dist-info`
（numpy-2.4.4、sherpa_onnx_core-2.0.0、click-8.5.0）交叉核对一致（2026-11-01）。

---

## 四、项目自身代码

- 许可：**MIT**，见仓库根 `LICENSE`（© 2026 妙语 MiaoYu contributors）。
- 测试音频 `tests/fixtures/*.wav` 由 Windows SAPI 合成，仅测试用。

---

## 五、复核方法

```powershell
# 1) 校验发布 zip（发布页附带 SHA256SUMS.txt）
Get-FileHash .\MiaoYu-v0.1.0-win-x64-full.zip -Algorithm SHA256
Get-FileHash .\MiaoYu-v0.1.0-win-x64-lite.zip -Algorithm SHA256

# 2) 校验包内模型与 ffmpeg（解压后进入包根目录）
Get-FileHash .\models\model.int8.onnx -Algorithm SHA256
Get-FileHash .\models\tokens.txt -Algorithm SHA256
Get-FileHash .\models\silero\silero_vad.onnx -Algorithm SHA256
Get-FileHash .\bin\ffmpeg.exe -Algorithm SHA256

# 3) 复核 FFmpeg 许可（应输出 --enable-version3，且不含 --enable-gpl / --enable-nonfree）
.\bin\ffmpeg.exe -version
```

---

## 六、变更记录

| 日期 | 内容 |
| --- | --- |
| 2026-11-01 | 首次生成：锁定 SenseVoice ONNX 模型与 Silero VAD 的 SHA-256；下载并校验 BtbN `n8.1` LGPL shared 构建（替换旧 GPL 构建）；补齐 `THIRD_PARTY_LICENSES/`；给出打包脚本与校验方法。 |
