# FFmpeg（LGPL 构建）来源与源码说明

本发布包内置 `bin\` 目录下的 FFmpeg 可执行文件与动态库，来自 **BtbN/FFmpeg-Builds** 的
**LGPL shared** 官方自动构建（**不是** GPL 构建）。以下数据均为 2026-11-01 在本机实测/抓取所得。

---

## 一、构建来源

| 项目 | 实测值 |
| --- | --- |
| 发布仓库 | <https://github.com/BtbN/FFmpeg-Builds> |
| 固定（不可变）Release tag | `autobuild-2026-10-31-12-59` |
| 资产文件名 | `ffmpeg-n8.1.3-27-g8dca1586f4-win64-lgpl-shared-8.1.zip` |
| 资产大小（字节） | `80356594` |
| 资产 SHA-256（GitHub Release API `digest` 字段） | `5cef7bc48e513f3cee7c881e8b2999d835541edb1846c3e89443a67f8f8556ab` |
| 本机下载文件 SHA-256（实算） | `5cef7bc48e513f3cee7c881e8b2999d835541edb1846c3e89443a67f8f8556ab`（与上一致，校验通过） |
| 直连下载 URL | <https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-10-31-12-59/ffmpeg-n8.1.3-27-g8dca1586f4-win64-lgpl-shared-8.1.zip> |
| 本机实际使用的镜像 URL | `https://gh-proxy.com/` + 上面直连 URL（GitHub 直连本机不可用；镜像仅做转发，文件 SHA-256 已与 GitHub API 的 digest 校验一致） |
| 抓取时间 | 2026-11-01（Asia/Shanghai） |

## 二、版本信息（`bin\ffmpeg.exe -version` 原样输出，2026-11-01 本机实测）

```text
ffmpeg version n8.1.3-27-g8dca1586f4-20261031 Copyright (c) 2000-2026 the FFmpeg developers
built with gcc 16.2.0 (crosstool-NG 1.29.0.7_b1a94f6)
configuration: --prefix=/ffbuild/prefix ... --target-os=mingw32 --enable-version3 --disable-debug --enable-shared --disable-static ... （完整 configuration 行见本节末）
libavutil      60. 26.103 / 60. 26.103
libavcodec     62. 28.103 / 62. 28.103
libavformat    62. 12.103 / 62. 12.103
libavdevice    62.  3.103 / 62.  3.103
libavfilter    11. 14.103 / 11. 14.103
libswscale      9.  5.103 /  9.  5.103
libswresample   6.  3.103 /  6.  3.103
```

### LGPL 判定证据（configuration 中的关键项，原样）

- **无** `--enable-gpl`
- **无** `--enable-nonfree`
- **有** `--enable-version3`（因此 FFmpeg 的 LGPL 部分按 **LGPL v3** 授权；构建包内 `LICENSE.txt` 即 LGPLv3 全文）
- GPL-only 组件被**明确关闭**：`--disable-libx264`、`--disable-libx265`、`--disable-libxavs2`、`--disable-libxvid`

> 说明：FFmpeg 以 `--enable-gpl` 编译时其配置行会包含该开关，且镜像内会带 x264/x265 等 GPL 库。
> 本机原 `build\dist\` 里预置的 FFmpeg **曾是 GPL 构建**（`--enable-gpl --enable-version3`，
> 见 `ffmpeg version N-126856-ged27b2c498-20260925`），**已在本发布包中被上述 LGPL 构建整体替换**。

## 三、许可

- 许可：**GNU Lesser General Public License v3**（LGPLv3；因 `--enable-version3`）。
- 许可全文：同目录 `FFmpeg-LGPL.txt`（取自构建包内 `LICENSE.txt`，未改动）。
  另附 `FFmpeg-LGPLv2.1-supplement.txt`（FFmpeg 上游基础许可 LGPLv2.1 全文）与
  `FFmpeg-GPLv3-supplement.txt`（LGPLv3 条款引用 GPLv3，故一并提供），均取自 FFmpeg 官方仓库。
- 允许再分发与动态链接闭源程序；要求保留许可文本、标明修改（本次未修改二进制），并按 LGPL 提供对应源码。

## 四、对应源码（LGPL 合规：如何获取对应源代码）

| 项目 | 值 |
| --- | --- |
| FFmpeg 版本 | `n8.1.3-27-g8dca1586f4`（`n8.1.3` 之后 27 个提交） |
| 对应源码 commit | `8dca1586f468bfcbb78ae3ed618af1361d9f140a`（2026-10-13，经 GitHub API `repos/FFmpeg/FFmpeg/commits/8dca1586f4` 实测） |
| 源码归档（对应 commit） | <https://github.com/FFmpeg/FFmpeg/archive/8dca1586f468bfcbb78ae3ed618af1361d9f140a.tar.gz> |
| 上游仓库 | <https://github.com/FFmpeg/FFmpeg> · <https://ffmpeg.org/download.html> |
| 构建脚本 | <https://github.com/BtbN/FFmpeg-Builds>（该 release tag 对应的自动构建脚本，含完整 `configure` 参数） |
| 本发布包内的 FFmpeg 二进制 | **未做任何修改**，直接来自上述 zip 的 `bin\`（仅剔除与本应用无关的 `ffplay.exe`） |

如需本发布包所用的对应源码副本，可按上述 commit 自行导出，或联系发布者索取（承诺自发布起
至少 3 年内有效）。

## 五、本发布包实际随包的 FFmpeg 文件

| 文件 | 字节 |
| --- | --- |
| `bin\ffmpeg.exe` | 552960 |
| `bin\ffprobe.exe` | 265216 |
| `bin\avcodec-62.dll` | 91620864 |
| `bin\avdevice-62.dll` | 5008384 |
| `bin\avfilter-11.dll` | 31893504 |
| `bin\avformat-62.dll` | 19846144 |
| `bin\avutil-60.dll` | 3021824 |
| `bin\swresample-6.dll` | 747008 |
| `bin\swscale-9.dll` | 12972544 |

（不随包：`ffplay.exe` —— 本应用不使用；`doc\`、`include\`、`lib\`、`presets\` —— 仅开发所需。）

## 六、功能性自检

2026-11-01 在解压目录内实测：

- `ffmpeg.exe -version` → exit 0，输出见第二节。
- `ffmpeg.exe -hide_banner -f dshow -list_devices true -i dummy` → exit 0（0.32 s），
  成功枚举本机音频/视频输入设备，说明 shared 版 DLL 依赖完整、dshow 输入可用。
