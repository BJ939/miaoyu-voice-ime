# 第三方许可与来源清单（THIRD_PARTY_LICENSES）

本目录收录 妙语 MiaoYu（voice-ime）v0.1.0 发布包中**分发的第三方组件**的许可全文与来源记录。
所有文件均为 **2026-11-01（Asia/Shanghai）在本机实测抓取**，未做内容修改；抓取方式与来源 URL 见下表。

## 一、许可文件清单

| 文件 | 字节 | 对应组件 | 来源 URL | 抓取方式 / 实测时间 |
| --- | ---: | --- | --- | --- |
| `FunASR-Model-License-1.1.txt` | 5306 | SenseVoice 模型权重（FunASR Model License v1.1，中英双语全文） | <https://github.com/modelscope/FunASR/blob/main/MODEL_LICENSE> | `gh api`（raw，已认证），2026-11-01 |
| `Apache-2.0.txt` | 11358 | Apache License 2.0 全文 | <https://www.apache.org/licenses/LICENSE-2.0.txt> | `curl` 直连，HTTP 200，2026-11-01 |
| `sherpa-onnx-Apache-2.0.txt` | 11560 | sherpa-onnx 推理运行时（Apache-2.0） | <https://github.com/k2-fsa/sherpa-onnx/blob/master/LICENSE> | `gh api`（raw），2026-11-01 |
| `Silero-VAD-MIT.txt` | 1097 | Silero VAD 模型（MIT） | <https://github.com/snakers4/silero-vad/blob/master/LICENSE> | `gh api`（raw），2026-11-01 |
| `FFmpeg-LGPL.txt` | 7651 | FFmpeg 动态库/可执行文件（LGPL v3） | 取自所分发构建包内 `LICENSE.txt`（见 `FFmpeg-来源与源码说明.md`） | 解压 BtbN 官方 zip 后读取，2026-11-01 |
| `FFmpeg-LGPLv2.1-supplement.txt` | 27019 | FFmpeg 上游基础许可 LGPL v2.1 全文 | <https://github.com/FFmpeg/FFmpeg/blob/master/COPYING.LGPLv2.1> | `gh api`（raw），2026-11-01 |
| `FFmpeg-GPLv3-supplement.txt` | 35821 | LGPLv3 条款所引用的 GPL v3 全文 | <https://github.com/FFmpeg/FFmpeg/blob/master/COPYING.GPLv3> | `gh api`（raw），2026-11-01 |
| `FFmpeg-来源与源码说明.md` | —（本目录说明文档） | FFmpeg 构建来源、版本、非 GPL 证据、对应源码获取方式 | 见文档内 | 2026-11-01 |
| `Python-deps.txt` | —（本目录说明文档） | Python 依赖实测版本与许可汇总 | 见文档内 | 2026-11-01 |
| `python\*.txt` / `python\Tcl-Tk-license.terms` | 见 `Python-deps.txt` | 随包分发的 Python 组件许可全文（12 个文件） | PyPI wheel 的 `*.dist-info\licenses\`，`C:\Python314\LICENSE.txt`，Tcl/Tk `license.terms` | 本机实测复制，2026-11-01 |

## 二、模型许可的两种上游表述（如实记录）

SenseVoice 权重的许可在上游存在**两种表述**，本发布包按**更严格者**执行：

1. **ModelScope 模型卡片**：`iic/SenseVoiceSmall` 的 `License` 字段为 **`Apache License 2.0`**
   （实测：`https://modelscope.cn/api/v1/models/iic/SenseVoiceSmall`，HTTP 200，19,371 字节，
   2026-11-01；返回 JSON 中 `Data.License = "Apache License 2.0"`）。
2. **FunASR 官方模型许可**：FunASR 仓库将其模型（含 SenseVoice）置于 **FunASR Model License v1.1**
   （实测抓取全文见 `FunASR-Model-License-1.1.txt`；该许可允许使用/修改/分享，但**要求署名**来源与作者、
   保留模型名称）。

→ 发布包与 `README.md` / `RELEASE_NOTES` 一律声明为 **FunASR Model License v1.1** 并署名
Alibaba / FunAudioLLM + k2-fsa/sherpa-onnx（ONNX 转换发布方），以同时满足两种表述中的更严格义务。
`Apache-2.0.txt` 一并随包，以备上游最终以 Apache-2.0 为准时同样满足要求。

## 三、随包组件与许可对应关系（速查）

| 分发物 | 组件 | 许可 | 许可文件 |
| --- | --- | --- | --- |
| `models\model.int8.onnx`、`models\tokens.txt` | SenseVoice（ONNX 版，k2-fsa/sherpa-onnx 转换发布） | FunASR Model License v1.1（另见上游 Apache-2.0 表述） | `FunASR-Model-License-1.1.txt`、`Apache-2.0.txt` |
| `models\silero\silero_vad.onnx` | Silero VAD | MIT | `Silero-VAD-MIT.txt` |
| `bin\ffmpeg.exe`、`bin\ffprobe.exe`、`bin\av*.dll`、`bin\sw*.dll` | FFmpeg（LGPL 构建） | LGPL v3 | `FFmpeg-LGPL.txt` 等 |
| `_internal\sherpa_onnx*`、`_internal\sherpa_onnx_core*` | sherpa-onnx | Apache-2.0 | `sherpa-onnx-Apache-2.0.txt` |
| `_internal\numpy*` | numpy | BSD-3-Clause 等 | `python\numpy-2.4.4-BSD-3-Clause.txt` |
| `_internal\PIL` | Pillow | MIT-CMU | `python\Pillow-12.2.0-MIT-CMU.txt` |
| `MiaoYu.exe`（内嵌 PYZ） | pynput | LGPLv3 | `python\pynput-1.8.1-LGPLv3.txt` |
| `MiaoYu.exe`（bootloader） | PyInstaller | GPLv2+ 例外（允许分发非自由程序） | `python\PyInstaller-6.22.3-bootloader-GPLv2-with-exception.txt` |
| `_internal\python314.dll` 等 | CPython 3.14 | PSF-2.0 | `python\Python-3.14-PSF.txt` |
| `_internal\_tk_data` 等 | Tcl/Tk | Tcl/Tk license | `python\Tcl-Tk-license.terms` |

## 四、说明

- 本目录只收录**随发布包分发**的第三方组件；仅用于构建、不随包分发的工具（如 git、编译器）不在其列。
  例外：PyInstaller 的 bootloader 被嵌入 `MiaoYu.exe`，故其许可一并收录。
- 项目自身代码与文档为 MIT，见仓库根 `LICENSE`。
- 若后续升级依赖或替换 FFmpeg 构建，**必须同步更新**本清单、对应许可文件与仓库根 `PROVENANCE.md`。
