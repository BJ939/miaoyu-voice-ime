#requires -Version 7.0
<#
.SYNOPSIS
    妙语 MiaoYu（voice-ime）发布包组装脚本 —— full / lite 双包 + SHA256SUMS。

.DESCRIPTION
    把「预构建 EXE 目录 + SenseVoice 模型 + LGPL 版 FFmpeg + 第三方许可 + PROVENANCE」
    组装成可直接在 Windows 10/11 x64 上解压即用的发布 zip：

        MiaoYu-v<ver>-win-x64-full.zip   含 models/（SenseVoice ONNX + tokens + Silero VAD）
        MiaoYu-v<ver>-win-x64-lite.zip   不含 models/（用户自备模型）
        SHA256SUMS.txt                   上面两个 zip 的 SHA-256

    zip 内顶层为单一目录 `MiaoYu-v<ver>-win-x64-<flavor>/`，解压后双击 `MiaoYu.exe` 即用。
    组装时会：
      * 剔除 EXE 目录里的用户运行数据（config.json / logs/ 等），避免泄露个人路径；
      * **整体替换 bin/ 为 LGPL 版 FFmpeg**（并断言 `ffmpeg -version` 的 configuration 不含
        `--enable-gpl` / `--enable-nonfree`，否则直接失败）；
      * 附带 THIRD_PARTY_LICENSES/、PROVENANCE.md、LICENSE、README.md、README_EN.md、
        docs/（DESIGN、截图、发布说明）、RELEASE_NOTES.md；
      * 校验 zip 结构与内容（无 config.json / logs/、无 *.log、full 包含模型文件）。

.PARAMETER Version
    发布版本号，默认 0.1.0。用于 zip 名与包内目录名。

.PARAMETER DistDir
    PyInstaller 预构建产物目录（内含 MiaoYu.exe），默认 build/dist/MiaoYu。
    该目录由 build/build_exe.ps1 生成；本脚本不重建 EXE。

.PARAMETER ModelsDir
    模型目录，默认 <repo>/models。full 包需要其中的 model*.onnx + tokens.txt（+ 可选 silero/silero_vad.onnx）。

.PARAMETER FfmpegRoot
    LGPL 版 FFmpeg 解压根目录（含 bin/ffmpeg.exe）。省略时自动在 -FfmpegCache 下探测；
    仍找不到且未给 -FetchFfmpeg，则报错并提示获取方式。

.PARAMETER FfmpegCache
    LGPL FFmpeg 缓存目录，默认 %LOCALAPPDATA%\Temp\voiceime_preview\ffmpeg-lgpl。

.PARAMETER FetchFfmpeg
    缓存缺失时，按固定 URL 下载 **BtbN 官方 LGPL shared** 构建（80,356,594 字节，
    SHA-256 5cef7bc4…56ab），多镜像 + 16 路分块并行下载并强制校验 SHA-256、解压到缓存目录。
    已知镜像顺序见 $FF_PIN.mirrorPrefixes；GitHub 直连不通时自动改用镜像。

.PARAMETER LicensesDir
    第三方许可目录，默认 <repo>/THIRD_PARTY_LICENSES。

.PARAMETER Provenance
    来源与完整性记录，默认 <repo>/PROVENANCE.md。

.PARAMETER OutDir
    输出目录（zip + SHA256SUMS.txt + release-manifest.json），默认 <repo>/build/work/release-out。
    默认放在 build/work 下是因为它被 .gitignore 忽略，避免误提交发布产物。

.PARAMETER StageDir
    组装用临时目录，默认 <repo>/build/work/release-stage。

.PARAMETER Full
    只做 full 包。（与 -Lite 都不给时 = 两个包都做。）

.PARAMETER Lite
    只做 lite 包。

.PARAMETER WithFFplay
    额外随包 ffplay.exe（默认不带：本应用不使用，省约 18 MB）。

.PARAMETER Compression
    zip 压缩级别：Optimal（默认）/ SmallestSize / Fastest。

.PARAMETER DryRun
    只组装目录 + 生成文件清单（含每个文件的 SHA-256）+ 打印体积，**不压缩、不写 SHA256SUMS**。
    需求确认阶段用；`-KeepStaging` 可保留组装目录供人工核对。

.PARAMETER KeepStaging
    打包完成后保留组装目录（默认成功后删除；DryRun 时默认保留）。

.PARAMETER SkipFfmpegCheck
    跳过「stage 内 ffmpeg 非 GPL」断言（**不建议**；仅在无执行权限的受限环境使用）。

.PARAMETER Strict
    把「个人路径/构建路径泄漏」类告警升级为失败。

.PARAMETER Chunks
    -FetchFfmpeg 时的分块并行数，默认 16。

.EXAMPLE
    # 只预览：组装目录 + 清单，不出 zip
    pwsh -File build/package_release.ps1 -Version 0.1.0 -DryRun

.EXAMPLE
    # 正式出两个包（先确认 build/dist/MiaoYu/MiaoYu.exe 已由 build/build_exe.ps1 生成）
    pwsh -File build/package_release.ps1 -Version 0.1.0

.EXAMPLE
    # 另一台机器上从零获取 LGPL FFmpeg 再打包
    pwsh -File build/package_release.ps1 -Version 0.1.0 -FetchFfmpeg

.NOTES
    本脚本只读 build/dist/、models/、THIRD_PARTY_LICENSES/、PROVENANCE.md，
    不修改 Release/、不重建 EXE、不 commit 任何东西。
    实测环境：PowerShell 7.6.9 / Windows 10-11 x64；FFmpeg 为 BtbN n8.1 LGPL shared 构建。
#>
[CmdletBinding()]
param(
    [string]$Version = '0.1.0',
    [string]$DistDir,
    [string]$ModelsDir,
    [string]$FfmpegRoot,
    [string]$FfmpegCache,
    [switch]$FetchFfmpeg,
    [string]$LicensesDir,
    [string]$Provenance,
    [string]$OutDir,
    [string]$StageDir,
    [switch]$Full,
    [switch]$Lite,
    [switch]$WithFFplay,
    [ValidateSet('Optimal', 'SmallestSize', 'Fastest')]
    [string]$Compression = 'Optimal',
    [switch]$DryRun,
    [switch]$KeepStaging,
    [switch]$SkipFfmpegCheck,
    [switch]$Strict,
    [int]$Chunks = 16
)

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# 固定常量：LGPL FFmpeg 资产（BtbN/FFmpeg-Builds）
# 数据来源：gh release view --repo BtbN/FFmpeg-Builds --json assets（2026-11-01 抓取）
# ---------------------------------------------------------------------------
$FF_PIN = @{
    tag            = 'autobuild-2026-10-31-12-59'
    asset          = 'ffmpeg-n8.1.3-27-g8dca1586f4-win64-lgpl-shared-8.1.zip'
    size           = 80356594
    sha256         = '5cef7bc48e513f3cee7c881e8b2999d835541edb1846c3e89443a67f8f8556ab'
    url            = 'https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-10-31-12-59/ffmpeg-n8.1.3-27-g8dca1586f4-win64-lgpl-shared-8.1.zip'
    rootName       = 'ffmpeg-n8.1.3-27-g8dca1586f4-win64-lgpl-shared-8.1'
    version        = 'n8.1.3-27-g8dca1586f4-20261031'
    mirrorPrefixes = @('https://gh-proxy.com/', 'https://ghproxy.net/', 'https://ghfast.top/', '')
}

function Write-Step([string]$Message) { Write-Host "`n==> $Message" -ForegroundColor Cyan }
function Write-Info([string]$Message) { Write-Host "    $Message" }
function Write-Ok([string]$Message) { Write-Host "    [OK] $Message" -ForegroundColor Green }
function Write-Warn2([string]$Message) { Write-Host "    [WARN] $Message" -ForegroundColor Yellow }
function Fail([string]$Message) {
    Write-Host "`n[FAIL] $Message" -ForegroundColor Red
    exit 1
}

function Get-RepoRoot { return (Split-Path -Parent $PSScriptRoot) }
function Format-MB([int64]$Bytes) { return ('{0:N1} MB' -f ($Bytes / 1MB)) }

# 递归统计目录：返回 @{ Files; Bytes }
function Get-DirStat([string]$Path) {
    $files = @(Get-ChildItem -LiteralPath $Path -Recurse -File -Force -ErrorAction SilentlyContinue)
    $sum = ($files | Measure-Object -Property Length -Sum).Sum
    if ($null -eq $sum) { $sum = 0 }
    return @{ Files = $files.Count; Bytes = [int64]$sum }
}

# 在二进制文件里查找 ASCII 子串（用于检查 EXE 是否残留本机构建路径）
function Test-BinaryContains([string]$FilePath, [string]$Needle) {
    try {
        $bytes = [System.IO.File]::ReadAllBytes($FilePath)
        $text = [System.Text.Encoding]::Latin1.GetString($bytes)
        return $text.Contains($Needle)
    } catch {
        return $false
    }
}

# 断言 ffmpeg 为 LGPL 构建：configuration 不含 --enable-gpl / --enable-nonfree
function Assert-LgplFfmpeg([string]$FfmpegExe) {
    if (-not (Test-Path -LiteralPath $FfmpegExe)) { Fail "找不到 ffmpeg.exe：$FfmpegExe" }
    $out = & $FfmpegExe -version 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) { Fail "ffmpeg.exe 无法运行（exit=$LASTEXITCODE）：$FfmpegExe" }
    $first = ($out -split "`n")[0].Trim()
    $configLine = (($out -split "`n") | Where-Object { $_ -match '^\s*configuration:' } | Select-Object -First 1)
    if ($configLine -match '--enable-gpl\b') {
        Fail ("FFmpeg 是 GPL 构建，不能用于发布包！`n        首行：{0}`n        命中：--enable-gpl`n        请改用 -FfmpegRoot 指向 LGPL 构建（或 -FetchFfmpeg 重新获取）。" -f $first)
    }
    if ($configLine -match '--enable-nonfree\b') {
        Fail ("FFmpeg 配置含 --enable-nonfree，不可再分发：{0}" -f $first)
    }
    $licenseBit = if ($configLine -match '--enable-version3\b') { 'LGPLv3 (--enable-version3)' } else { 'LGPLv2.1+' }
    Write-Ok "FFmpeg 非 GPL 校验通过：$first"
    Write-Info "许可推断：$licenseBit；无 --enable-gpl / --enable-nonfree"
    return $first
}

# ---------------------------------------------------------------------------
# LGPL FFmpeg：定位 / 下载（多镜像 + 分块并行 + SHA-256 强校验）/ 解压
# ---------------------------------------------------------------------------
function Find-FfmpegRoot([string]$Explicit, [string]$Cache) {
    if ($Explicit) {
        if (Test-Path -LiteralPath (Join-Path $Explicit 'bin\ffmpeg.exe')) { return (Resolve-Path -LiteralPath $Explicit).Path }
        if (Test-Path -LiteralPath (Join-Path $Explicit 'ffmpeg.exe')) { return (Resolve-Path -LiteralPath $Explicit).Path }
        Fail "指定的 -FfmpegRoot 下找不到 bin\ffmpeg.exe 或 ffmpeg.exe：$Explicit"
    }
    if ($Cache -and (Test-Path -LiteralPath $Cache)) {
        if (Test-Path -LiteralPath (Join-Path $Cache 'bin\ffmpeg.exe')) { return (Resolve-Path -LiteralPath $Cache).Path }
        $hit = Get-ChildItem -LiteralPath $Cache -Directory -Force -ErrorAction SilentlyContinue |
            Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'bin\ffmpeg.exe') } |
            Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }
    return $null
}

function Invoke-ChunkedDownload {
    param([string]$Url, [string]$OutFile, [int64]$Size, [string]$Sha256, [int]$ChunkCount, [string]$ChunkDir)
    New-Item -ItemType Directory -Force -Path $ChunkDir, (Split-Path -Parent $OutFile) | Out-Null
    Remove-Item (Join-Path $ChunkDir 'chunk_*.bin') -Force -ErrorAction SilentlyContinue
    $chunkBytes = [int64][math]::Ceiling($Size / $ChunkCount)
    Write-Info "分块下载：$ChunkCount 块 × 约 $(Format-MB $chunkBytes)，源 $Url"

    $results = 0..($ChunkCount - 1) | ForEach-Object -Parallel {
        $idx        = $_
        $url        = $using:Url
        $chunkDir   = $using:ChunkDir
        $chunkBytes = [int64]$using:chunkBytes
        $total      = [int64]$using:Size
        $start      = [int64]$idx * $chunkBytes
        $end        = [math]::Min($start + $chunkBytes - 1, $total - 1)
        $target     = Join-Path $chunkDir ("chunk_{0:D2}.bin" -f $idx)
        $want       = $end - $start + 1
        for ($attempt = 1; $attempt -le 6; $attempt++) {
            & curl.exe -sSL --connect-timeout 15 --max-time 900 --retry 2 --retry-delay 2 `
                -r "$start-$end" -o $target $url 2>$null | Out-Null
            if (Test-Path -LiteralPath $target) {
                $len = (Get-Item -LiteralPath $target).Length
                if ($len -eq $want) { return "OK $idx $len" }
            }
            Start-Sleep -Seconds ([math]::Min(2 * $attempt, 12))
        }
        $len = if (Test-Path -LiteralPath $target) { (Get-Item -LiteralPath $target).Length } else { 0 }
        return "FAIL $idx $len/$want"
    } -ThrottleLimit $ChunkCount

    $failed = @($results | Where-Object { $_ -like 'FAIL*' })
    if ($failed.Count -gt 0) {
        Write-Warn2 "分块下载失败 $($failed.Count) 块：$($failed -join '; ')"
        return $false
    }

    $fs = [System.IO.File]::Open($OutFile, [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write)
    try {
        for ($i = 0; $i -lt $ChunkCount; $i++) {
            $p = Join-Path $ChunkDir ("chunk_{0:D2}.bin" -f $i)
            $bytes = [System.IO.File]::ReadAllBytes($p)
            $fs.Write($bytes, 0, $bytes.Length)
        }
    } finally { $fs.Dispose() }
    Remove-Item (Join-Path $ChunkDir 'chunk_*.bin') -Force -ErrorAction SilentlyContinue

    if ((Get-Item -LiteralPath $OutFile).Length -ne $Size) {
        Write-Warn2 "下载文件大小不符：$((Get-Item -LiteralPath $OutFile).Length) != $Size"
        return $false
    }
    $hash = (Get-FileHash -LiteralPath $OutFile -Algorithm SHA256).Hash.ToLower()
    if ($hash -ne $Sha256) {
        Write-Warn2 "SHA-256 校验失败：$hash != $Sha256"
        return $false
    }
    return $true
}

function Install-FfmpegLgpl([string]$Cache, [int]$ChunkCount) {
    New-Item -ItemType Directory -Force -Path $Cache | Out-Null
    $zipPath = Join-Path $Cache $FF_PIN.asset
    $chunkDir = Join-Path $Cache '.chunks'
    $ok = $false

    if ((Test-Path -LiteralPath $zipPath) -and ((Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLower() -eq $FF_PIN.sha256)) {
        Write-Ok "缓存命中且 SHA-256 一致：$zipPath"
        $ok = $true
    } else {
        foreach ($prefix in $FF_PIN.mirrorPrefixes) {
            $url = "$prefix$($FF_PIN.url)"
            $label = if ($prefix) { $prefix } else { 'GitHub 直连' }
            Write-Info "尝试下载源：$label"
            if (Invoke-ChunkedDownload -Url $url -OutFile $zipPath -Size $FF_PIN.size -Sha256 $FF_PIN.sha256 -ChunkCount $ChunkCount -ChunkDir $chunkDir) {
                Write-Ok "下载成功且 SHA-256 校验一致（源：$label）"
                $ok = $true
                break
            }
        }
    }
    if (-not $ok) {
        Fail @"
LGPL FFmpeg 下载失败（所有镜像都未通过校验）。
    手动获取方式：
      1) 下载 $($FF_PIN.url)
         镜像前缀可用 gh-proxy.com/ 、ghproxy.net/ 等；
      2) 校验 SHA-256 必须为 $($FF_PIN.sha256)（$($FF_PIN.size) 字节）；
      3) 解压到 $Cache 下，使 $Cache\$($FF_PIN.rootName)\bin\ffmpeg.exe 存在；
      4) 重跑本脚本。
"@
    }

    Write-Step '解压 LGPL FFmpeg'
    $target = Join-Path $Cache $FF_PIN.rootName
    if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force }
    Expand-Archive -LiteralPath $zipPath -DestinationPath $Cache -Force
    if (-not (Test-Path -LiteralPath (Join-Path $target 'bin\ffmpeg.exe'))) {
        $hit = Get-ChildItem -LiteralPath $Cache -Directory | Where-Object { $_.Name -like 'ffmpeg-*' -and (Test-Path (Join-Path $_.FullName 'bin\ffmpeg.exe')) } | Select-Object -First 1
        if ($hit) { $target = $hit.FullName } else { Fail "解压后未找到 bin\ffmpeg.exe：$Cache" }
    }
    Write-Ok "已解压：$target"
    return $target
}

# 检查文本文件里是否残留本机绝对路径（发布前隐私闸门，告警级）
function Find-PersonalPathLeak([string]$Path) {
    $patterns = @(
        '(?i)\b[A-Z]:\\Users\\[^\\\s"'']+',      # 个人用户目录
        '(?i)\b[A-Z]:\\StockRelated',              # 本仓库所在盘符路径
        '(?i)\bC:\\Python\d+\\python\.exe'         # 仅告警的构建机路径（一般无害）
    )
    $hits = New-Object System.Collections.Generic.List[string]
    $files = @(Get-ChildItem -LiteralPath $Path -Recurse -File -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.Extension -in @('.md', '.txt', '.json', '.ps1', '.vbs', '.bat', '.cmd', '.cfg', '.ini', '.log') })
    foreach ($f in $files) {
        try {
            $content = Get-Content -LiteralPath $f.FullName -Raw -ErrorAction Stop
        } catch { continue }
        if (-not $content) { continue }
        foreach ($p in $patterns) {
            if ($content -match $p) {
                $rel = $f.FullName.Substring($Path.Length).TrimStart('\')
                $hits.Add("$rel -> $p")
                break
            }
        }
    }
    return $hits
}

# ---------------------------------------------------------------------------
# 组装一个 flavor
# ---------------------------------------------------------------------------
function New-PackageStage {
    param(
        [string]$Flavor,
        [string]$TopName,
        [string]$AppRoot,
        [string]$Repo,
        [string]$DistDir,
        [string]$ModelsDir,
        [string]$FfmpegBin,
        [string]$LicensesDir,
        [string]$Provenance,
        [switch]$WithFFplay
    )
    if (Test-Path -LiteralPath $AppRoot) { Remove-Item -LiteralPath $AppRoot -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $AppRoot | Out-Null

    Write-Info "复制 EXE 目录：$DistDir"
    Copy-Item -Path (Join-Path $DistDir '*') -Destination $AppRoot -Recurse -Force

    # 剔除用户运行数据（config.json / logs / backup 等），避免泄露个人路径
    $dropTop = @('config.json', 'config.json.tmp', 'logs', 'backup', 'logs.txt')
    foreach ($rel in $dropTop) {
        $p = Join-Path $AppRoot $rel
        if (Test-Path -LiteralPath $p) { Remove-Item -LiteralPath $p -Recurse -Force; Write-Info "剔除运行数据：$rel" }
    }
    Get-ChildItem -LiteralPath $AppRoot -File -Filter '*.log' -Force -ErrorAction SilentlyContinue |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force; Write-Info "剔除日志：$($_.Name)" }

    # 整体替换 bin/ 为 LGPL FFmpeg（杜绝旧 GPL DLL 残留）
    $binDst = Join-Path $AppRoot 'bin'
    if (Test-Path -LiteralPath $binDst) { Remove-Item -LiteralPath $binDst -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $binDst | Out-Null
    $ffFiles = @(Get-ChildItem -LiteralPath $FfmpegBin -File -Force)
    $copied = 0
    foreach ($f in $ffFiles) {
        if ($f.Name -eq 'ffplay.exe' -and -not $WithFFplay) { continue }
        Copy-Item -LiteralPath $f.FullName -Destination (Join-Path $binDst $f.Name) -Force
        $copied++
    }
    $binStat = Get-DirStat $binDst
    Write-Ok "bin/ 已替换为 LGPL FFmpeg：$copied 个文件，$(Format-MB $binStat.Bytes)（不含 ffplay.exe=$(-not $WithFFplay)）"

    # 许可与来源
    Write-Info '复制 THIRD_PARTY_LICENSES/ 与 PROVENANCE.md'
    Copy-Item -LiteralPath $LicensesDir -Destination (Join-Path $AppRoot 'THIRD_PARTY_LICENSES') -Recurse -Force
    Copy-Item -LiteralPath $Provenance -Destination (Join-Path $AppRoot 'PROVENANCE.md') -Force
    foreach ($doc in @('LICENSE', 'README.md', 'README_EN.md')) {
        $src = Join-Path $Repo $doc
        if (Test-Path -LiteralPath $src) { Copy-Item -LiteralPath $src -Destination (Join-Path $AppRoot $doc) -Force }
    }
    # docs/（DESIGN.md、截图、发布说明；约 0.6 MB，使包内 README 的相对链接尽量可用）
    $docsSrc = Join-Path $Repo 'docs'
    if (Test-Path -LiteralPath $docsSrc) {
        Copy-Item -LiteralPath $docsSrc -Destination (Join-Path $AppRoot 'docs') -Recurse -Force
        Write-Info '复制 docs/（DESIGN、截图、发布说明）'
    }
    $releaseNotes = Join-Path $Repo 'docs\RELEASE_NOTES_v0.1.0.md'
    if (Test-Path -LiteralPath $releaseNotes) { Copy-Item -LiteralPath $releaseNotes -Destination (Join-Path $AppRoot 'RELEASE_NOTES.md') -Force }

    # 模型
    if ($Flavor -eq 'full') {
        Write-Info '复制 SenseVoice 模型（full 包）'
        $modelSrc = $null
        foreach ($n in @('model.int8.onnx', 'model.onnx', 'model.fp32.onnx')) {
            $cand = Join-Path $ModelsDir $n
            if (Test-Path -LiteralPath $cand) { $modelSrc = $cand; break }
        }
        if (-not $modelSrc) { Fail "full 包需要模型：$ModelsDir 下未找到 model.int8.onnx / model.onnx / model.fp32.onnx" }
        $tokensSrc = Join-Path $ModelsDir 'tokens.txt'
        if (-not (Test-Path -LiteralPath $tokensSrc)) { Fail "full 包需要 tokens.txt：$tokensSrc" }
        $mDst = Join-Path $AppRoot 'models'
        New-Item -ItemType Directory -Force -Path $mDst | Out-Null
        Copy-Item -LiteralPath $modelSrc -Destination (Join-Path $mDst (Split-Path -Leaf $modelSrc)) -Force
        Copy-Item -LiteralPath $tokensSrc -Destination (Join-Path $mDst 'tokens.txt') -Force
        $vadSrc = Join-Path $ModelsDir 'silero\silero_vad.onnx'
        if (Test-Path -LiteralPath $vadSrc) {
            New-Item -ItemType Directory -Force -Path (Join-Path $mDst 'silero') | Out-Null
            Copy-Item -LiteralPath $vadSrc -Destination (Join-Path $mDst 'silero\silero_vad.onnx') -Force
        } else {
            Write-Warn2 '未找到 models\silero\silero_vad.onnx —— full 包将缺少 VAD 模型（建议补齐）'
        }
        $mStat = Get-DirStat $mDst
        Write-Ok "models/：$($mStat.Files) 个文件，$(Format-MB $mStat.Bytes)"
    } else {
        Write-Info 'lite 包：不包含 models/（用户自备模型）'
    }
    return $AppRoot
}

# 写文件清单（sha256 + size + 相对路径），用于 DryRun 预览
function Write-StageManifest {
    param([string]$AppRoot, [string]$OutFile)
    $lines = New-Object System.Collections.Generic.List[string]
    $files = @(Get-ChildItem -LiteralPath $AppRoot -Recurse -File -Force | Sort-Object FullName)
    $total = 0
    foreach ($f in $files) {
        $rel = $f.FullName.Substring($AppRoot.Length).TrimStart('\').Replace('\', '/')
        $hash = (Get-FileHash -LiteralPath $f.FullName -Algorithm SHA256).Hash.ToLower()
        $lines.Add(('{0}  {1,12}  {2}' -f $hash, $f.Length, $rel))
        $total += $f.Length
    }
    $header = New-Object System.Collections.Generic.List[string]
    $header.Add("# 妙语 MiaoYu 发布包文件清单（未压缩，供审计/比对；dry-run 时即预览清单）")
    $header.Add("# 生成时间：$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz')")
    $header.Add("# 顶层目录：$(Split-Path -Leaf $AppRoot)")
    $header.Add("# 文件数：$($files.Count)    总字节：$total")
    $header.Add('# 格式：<SHA-256>  <字节数>  <包内相对路径>')
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $OutFile) | Out-Null
    ($header + $lines) | Set-Content -LiteralPath $OutFile -Encoding utf8NoBOM
    return @{ Files = $files.Count; Bytes = [int64]$total }
}

# 校验 zip：顶层目录正确、无 config.json / logs / *.log、full 含模型、bin/ffmpeg.exe 存在
function Test-ReleaseZip {
    param([string]$ZipPath, [string]$TopName, [string]$Flavor)
    $zip = [System.IO.Compression.ZipFile]::OpenRead($ZipPath)
    try {
        $entries = @($zip.Entries)
        $problems = New-Object System.Collections.Generic.List[string]
        $outside = @($entries | Where-Object { -not $_.FullName.StartsWith("$TopName/") })
        if ($outside.Count -gt 0) { $problems.Add("有 $($outside.Count) 个条目不在顶层目录 $TopName/ 下") }
        foreach ($e in $entries) {
            $n = $e.FullName
            if ($n -eq "$TopName/config.json" -or $n -eq "$TopName/config.json.tmp") { $problems.Add("含用户配置：$n") }
            if ($n -like "$TopName/logs/*" -or $n -eq "$TopName/logs/") { $problems.Add("含日志目录：$n") }
            if ($n -like '*.log') { $problems.Add("含日志文件：$n") }
        }
        if (-not ($entries | Where-Object { $_.FullName -eq "$TopName/MiaoYu.exe" })) { $problems.Add('缺少 MiaoYu.exe') }
        if (-not ($entries | Where-Object { $_.FullName -eq "$TopName/bin/ffmpeg.exe" })) { $problems.Add('缺少 bin/ffmpeg.exe') }
        if (-not ($entries | Where-Object { $_.FullName -eq "$TopName/PROVENANCE.md" })) { $problems.Add('缺少 PROVENANCE.md') }
        if (-not ($entries | Where-Object { $_.FullName -like "$TopName/THIRD_PARTY_LICENSES/*" })) { $problems.Add('缺少 THIRD_PARTY_LICENSES/') }
        if ($Flavor -eq 'full') {
            if (-not ($entries | Where-Object { $_.FullName -like "$TopName/models/*.onnx" })) { $problems.Add('full 包缺少 models/*.onnx') }
            if (-not ($entries | Where-Object { $_.FullName -eq "$TopName/models/tokens.txt" })) { $problems.Add('full 包缺少 models/tokens.txt') }
        } else {
            if ($entries | Where-Object { $_.FullName -like "$TopName/models/*" }) { $problems.Add('lite 包不应包含 models/') }
        }
        if ($problems.Count -gt 0) { Fail ("zip 校验失败：$ZipPath`n        - " + ($problems -join "`n        - ")) }
        return @{ Entries = $entries.Count }
    } finally { $zip.Dispose() }
}

# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
$repo = Get-RepoRoot
if (-not $DistDir) { $DistDir = Join-Path $repo 'build\dist\MiaoYu' }
if (-not $ModelsDir) { $ModelsDir = Join-Path $repo 'models' }
if (-not $FfmpegCache) { $FfmpegCache = Join-Path $env:LOCALAPPDATA 'Temp\voiceime_preview\ffmpeg-lgpl' }
if (-not $LicensesDir) { $LicensesDir = Join-Path $repo 'THIRD_PARTY_LICENSES' }
if (-not $Provenance) { $Provenance = Join-Path $repo 'PROVENANCE.md' }
if (-not $OutDir) { $OutDir = Join-Path $repo 'build\work\release-out' }
if (-not $StageDir) { $StageDir = Join-Path $repo 'build\work\release-stage' }

if ($Version -notmatch '^\d+\.\d+\.\d+([-+.][0-9A-Za-z.\-]+)?$') {
    Fail "-Version 格式不合法：'$Version'（应形如 0.1.0 或 0.1.0-rc1）"
}

$flavors = @()
if (-not $Full -and -not $Lite) { $flavors = @('full', 'lite') }
else { if ($Full) { $flavors += 'full' }; if ($Lite) { $flavors += 'lite' } }

Write-Host "`n========== 妙语 MiaoYu 发布打包 v$Version ==========" -ForegroundColor Magenta
Write-Info "仓库根：$repo"
Write-Info "EXE 目录：$DistDir"
Write-Info "模型目录：$ModelsDir"
Write-Info "输出目录：$OutDir"
Write-Info "组装目录：$StageDir"
Write-Info "包类型：$($flavors -join ' + ')$(if ($DryRun) { '（DryRun 预览，不压缩）' } else { '' })"

# ---- 前置校验 ----
Write-Step '前置校验'
$exePath = Join-Path $DistDir 'MiaoYu.exe'
if (-not (Test-Path -LiteralPath $exePath)) {
    Fail @"
找不到预构建 EXE：$exePath
    请先运行 build/build_exe.ps1（由 release-dev 在隐私改进完成后重建），
    或用 -DistDir 指向已有的 PyInstaller onedir 产物目录。
"@
}
$distStat = Get-DirStat $DistDir
Write-Ok "EXE 目录：$($distStat.Files) 个文件，$(Format-MB $distStat.Bytes)"
if (-not (Test-Path -LiteralPath $LicensesDir)) { Fail "缺少第三方许可目录：$LicensesDir" }
if (-not (Test-Path -LiteralPath $Provenance)) { Fail "缺少 PROVENANCE.md：$Provenance" }
Write-Ok "许可目录与 PROVENANCE.md 就绪"

# ---- 定位 LGPL FFmpeg ----
Write-Step '定位 LGPL 版 FFmpeg'
$ffRoot = Find-FfmpegRoot -Explicit $FfmpegRoot -Cache $FfmpegCache
if (-not $ffRoot) {
    if ($FetchFfmpeg) {
        Write-Info '缓存中未找到 LGPL FFmpeg，开始按固定 URL 下载（BtbN 官方构建）'
        $ffRoot = Install-FfmpegLgpl -Cache $FfmpegCache -ChunkCount $Chunks
    } else {
        Fail @"
未找到 LGPL 版 FFmpeg（含 bin\ffmpeg.exe 的目录）。
    已检查：-FfmpegRoot='$FfmpegRoot'、缓存 '$FfmpegCache'
    解决办法（任一）：
      a) 加 -FetchFfmpeg，脚本会自动从 BtbN/FFmpeg-Builds 下载固定版本的 LGPL shared 构建并校验 SHA-256；
      b) 手动下载并解压 $($FF_PIN.asset) 到 '$FfmpegCache'（SHA-256 必须为 $($FF_PIN.sha256)），
         使 '$FfmpegCache\$($FF_PIN.rootName)\bin\ffmpeg.exe' 存在；
      c) 用 -FfmpegRoot 指向已有的 LGPL 构建解压目录。
    注意：不能使用 GPL 构建（例如 C:\ffmpeg 下的 gpl-shared 版本）。
"@
    }
}
$ffBin = if (Test-Path -LiteralPath (Join-Path $ffRoot 'bin\ffmpeg.exe')) { Join-Path $ffRoot 'bin' }
        elseif (Test-Path -LiteralPath (Join-Path $ffRoot 'ffmpeg.exe')) { $ffRoot }
        else { Fail "FFmpeg 目录结构异常（找不到 ffmpeg.exe）：$ffRoot" }
$ffVersionLine = Assert-LgplFfmpeg -FfmpegExe (Join-Path $ffBin 'ffmpeg.exe')
$ffStat = Get-DirStat $ffBin
Write-Ok "FFmpeg bin：$ffRoot（$(Format-MB $ffStat.Bytes)）"

# ---- 组装 + 压缩 ----
$results = @()
$built = @()
foreach ($flavor in $flavors) {
    $topName = "MiaoYu-v$Version-win-x64-$flavor"
    $appRoot = Join-Path $StageDir $topName
    $zipPath = Join-Path $OutDir "$topName.zip"

    Write-Step "组装 $flavor 包"
    New-Item -ItemType Directory -Force -Path $StageDir, $OutDir | Out-Null
    New-PackageStage -Flavor $flavor -TopName $topName -AppRoot $appRoot -Repo $repo `
        -DistDir $DistDir -ModelsDir $ModelsDir -FfmpegBin $ffBin `
        -LicensesDir $LicensesDir -Provenance $Provenance -WithFFplay:$WithFFplay | Out-Null

    # stage 内 ffmpeg 复检（防止复制错目录）
    if (-not $SkipFfmpegCheck) {
        Assert-LgplFfmpeg -FfmpegExe (Join-Path $appRoot 'bin\ffmpeg.exe') | Out-Null
    } else {
        Write-Warn2 '已跳过 stage 内 FFmpeg 非 GPL 校验（-SkipFfmpegCheck）'
    }

    # 隐私闸门：运行数据残留 + 文本文件个人路径
    $leaks = @(Find-PersonalPathLeak -Path $appRoot)
    $exeLeak = Test-BinaryContains -FilePath (Join-Path $appRoot 'MiaoYu.exe') -Needle 'StockRelated'
    if ($leaks.Count -gt 0) {
        $msg = "stage 文本文件中疑似个人/构建路径：" + ($leaks -join '; ')
        if ($Strict) { Fail $msg } else { Write-Warn2 $msg }
    }
    if ($exeLeak) {
        $msg = 'MiaoYu.exe 内疑似残留本机构建路径 "StockRelated"（PyInstaller 可能保留 co_filename，发布前建议 release-dev 复核）'
        if ($Strict) { Fail $msg } else { Write-Warn2 $msg }
    }
    if ($leaks.Count -eq 0 -and -not $exeLeak) { Write-Ok '隐私闸门：未发现个人路径残留' }

    $stat = Get-DirStat $appRoot
    # 文件清单（dry-run 预览与正式打包都产出，便于审计/比对）
    $manifestPath = Join-Path $OutDir ("manifest-$flavor.txt")
    $man = Write-StageManifest -AppRoot $appRoot -OutFile $manifestPath
    Write-Ok "文件清单：$manifestPath（$($man.Files) 个文件，$(Format-MB $man.Bytes)）"
    if ($DryRun) {
        $results += [pscustomobject]@{
            Flavor = $flavor; Name = $topName; ZipBytes = $null; Sha256 = $null
            StageFiles = $stat.Files; StageBytes = $stat.Bytes; Status = 'dry-run'
        }
        continue
    }

    Write-Step "压缩 $topName.zip（$Compression）"
    New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
    if (Test-Path -LiteralPath $zipPath) { Remove-Item -LiteralPath $zipPath -Force }
    $level = [System.Enum]::Parse([System.IO.Compression.CompressionLevel], $Compression)
    [System.IO.Compression.ZipFile]::CreateFromDirectory($appRoot, $zipPath, $level, $true)
    $zipInfo = Test-ReleaseZip -ZipPath $zipPath -TopName $topName -Flavor $flavor
    $hash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLower()
    $zipBytes = (Get-Item -LiteralPath $zipPath).Length
    Write-Ok "已生成：$zipPath（$($zipInfo.Entries) 个条目，$(Format-MB $zipBytes)）"
    Write-Ok "SHA-256：$hash"
    $results += [pscustomobject]@{
        Flavor = $flavor; Name = $topName; ZipBytes = $zipBytes; Sha256 = $hash
        StageFiles = $stat.Files; StageBytes = $stat.Bytes; Status = 'built'
    }

    if (-not $KeepStaging) {
        Remove-Item -LiteralPath $appRoot -Recurse -Force -ErrorAction SilentlyContinue
        Write-Info '已清理组装目录（-KeepStaging 可保留）'
    }
}

# ---- SHA256SUMS / manifest.json ----
if (-not $DryRun) {
    Write-Step '写 SHA256SUMS.txt 与 release-manifest.json'
    $sumLines = @($results | Where-Object { $_.Status -eq 'built' } | ForEach-Object { "{0}  {1}.zip" -f $_.Sha256, $_.Name })
    $sumPath = Join-Path $OutDir 'SHA256SUMS.txt'
    $sumLines | Set-Content -LiteralPath $sumPath -Encoding utf8NoBOM
    Write-Ok "SHA256SUMS.txt：$sumPath"

    $manifest = [ordered]@{
        product   = '妙语 MiaoYu (voice-ime)'
        version   = $Version
        platform  = 'windows-x64'
        generated = (Get-Date -Format 'yyyy-MM-ddTHH:mm:sszzz')
        packages  = @($results | Where-Object { $_.Status -eq 'built' } | ForEach-Object {
            [ordered]@{ file = "$($_.Name).zip"; bytes = $_.ZipBytes; sha256 = $_.Sha256 }
        })
        ffmpeg    = [ordered]@{
            sourceTag = $FF_PIN.tag
            asset     = $FF_PIN.asset
            assetSha256 = $FF_PIN.sha256
            versionLine = $ffVersionLine
            license   = 'LGPL-3.0 (--enable-version3, no --enable-gpl)'
        }
    }
    $manifestPath = Join-Path $OutDir 'release-manifest.json'
    $manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $manifestPath -Encoding utf8NoBOM
    Write-Ok "release-manifest.json：$manifestPath"
}

# ---- 汇总 ----
Write-Host "`n========== 结果汇总 ==========" -ForegroundColor Magenta
$results | ForEach-Object {
    $zipText = if ($_.ZipBytes) { Format-MB $_.ZipBytes } else { '（未压缩）' }
    Write-Host ("  {0,-34} stage={1,10}  zip={2}" -f $_.Name, (Format-MB $_.StageBytes), $zipText)
}
if ($DryRun) {
    Write-Host "`n[DRY-RUN] 未生成 zip；组装目录保留在：$StageDir" -ForegroundColor Yellow
    Write-Host "          预览文件清单见输出目录的 manifest-<flavor>.txt。" -ForegroundColor Yellow
    Write-Host "          确认无误后去掉 -DryRun 重跑即可生成 zip + SHA256SUMS.txt。" -ForegroundColor Yellow
} else {
    Write-Host "`n输出目录：$OutDir" -ForegroundColor Green
    Write-Host "发布时请连同 SHA256SUMS.txt 一并上传（README/RELEASE_NOTES 中声明的校验值应与之一致）。" -ForegroundColor Green
}
exit 0
