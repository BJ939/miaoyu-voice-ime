# 妙语 MiaoYu（voice-ime）EXE 打包脚本（PyInstaller，onedir 模式）
# 用法：pwsh -File build_exe.ps1            # 打包
#      pwsh -File build_exe.ps1 -CleanOnly # 只清理 build/dist
param([switch]$CleanOnly)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$appDir = Split-Path -Parent $root
# 对外品牌名（EXE 产物名）：妙语 MiaoYu。
# 内部 Python 包名 / 单实例互斥体 / 命名事件仍是 voice-ime，不随 EXE 改名。
$appName = "MiaoYu"

if ($CleanOnly) {
    Remove-Item -Recurse -Force "$appDir\build\dist", "$appDir\build\work" -ErrorAction SilentlyContinue
    Write-Host "已清理 build/dist 与 build/work"
    return
}

# 1. 图标
python -B "$root\generate_icon.py"
if ($LASTEXITCODE -ne 0) { throw "图标生成失败" }

# 2. 清理旧产物
Remove-Item -Recurse -Force "$appDir\build\dist", "$appDir\build\work" -ErrorAction SilentlyContinue

# 3. 打包（工作目录=项目根，保证 app.py 相对导入）
Push-Location $appDir
try {
    python -m PyInstaller `
        --noconfirm --clean --windowed `
        --name $appName `
        --icon "$root\icon.ico" `
        --distpath "$appDir\build\dist" `
        --workpath "$appDir\build\work" `
        --specpath "$appDir\build" `
        --collect-all sherpa_onnx `
        --collect-submodules pynput `
        --hidden-import numpy `
        --hidden-import tkinter `
        --noupx `
        "$appDir\app.py"
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败" }
}
finally {
    Pop-Location
}

# 4. 结果
$exe = "$appDir\build\dist\$appName\$appName.exe"
if (-not (Test-Path $exe)) { throw "未找到产物：$exe" }
$size = (Get-ChildItem "$appDir\build\dist\$appName" -Recurse -File | Measure-Object -Property Length -Sum).Sum
Write-Host ("打包完成：{0}" -f $exe)
Write-Host ("目录总大小：{0:N1} MB" -f ($size / 1MB))

# 5. 同步到发布目录 Release（保留用户已有 config.json / logs）
$release = "$appDir\Release"
if (Test-Path $release) {
    Remove-Item -Recurse -Force "$release\_internal" -ErrorAction SilentlyContinue
    # 一并清理旧品牌 voice-ime.exe，避免 Release 里同时存在两个 exe（品牌统一为 MiaoYu.exe）
    Remove-Item -Force -ErrorAction SilentlyContinue "$release\voice-ime.exe", "$release\$appName.exe"
}
New-Item -ItemType Directory -Force -Path $release | Out-Null
Copy-Item -Recurse -Force "$appDir\build\dist\$appName\_internal" $release
Copy-Item -Force $exe $release
Write-Host ("已同步到发布目录：{0}" -f (Join-Path $release "$appName.exe"))
