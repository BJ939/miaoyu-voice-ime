# voice-ime 开机自启安装/卸载脚本（兼容 Windows PowerShell 5.1，文件 UTF-8 带 BOM）。
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File install_autostart.ps1
#       在 shell:startup 创建指向 run_voiceime.vbs 的快捷方式（静默开机自启）
#   powershell -NoProfile -ExecutionPolicy Bypass -File install_autostart.ps1 -Remove
#       删除该快捷方式（取消开机自启）
[CmdletBinding()]
param(
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$scriptDir = if ($PSCommandPath) {
    Split-Path -Parent $PSCommandPath
} else {
    Split-Path -Parent $MyInvocation.MyCommand.Path
}
$vbsPath = Join-Path $scriptDir 'run_voiceime.vbs'
$startupDir = [Environment]::GetFolderPath('Startup')
$shortcutPath = Join-Path $startupDir 'voice-ime.lnk'

Write-Host "voice-ime 开机自启脚本目录：$scriptDir"
Write-Host "启动脚本：$vbsPath"
Write-Host "启动文件夹：$startupDir"
Write-Host "快捷方式：$shortcutPath"

if ($Remove) {
    if (Test-Path -LiteralPath $shortcutPath) {
        Remove-Item -LiteralPath $shortcutPath -Force
        Write-Host "已删除开机自启快捷方式。"
    } else {
        Write-Host "未找到开机自启快捷方式，无需删除。"
    }
    exit 0
}

if (-not (Test-Path -LiteralPath $vbsPath)) {
    Write-Host "错误：启动脚本不存在：$vbsPath"
    exit 1
}
if (-not (Test-Path -LiteralPath $startupDir)) {
    New-Item -ItemType Directory -Path $startupDir -Force | Out-Null
}

$wsh = New-Object -ComObject WScript.Shell
$shortcut = $wsh.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $vbsPath
$shortcut.WorkingDirectory = $scriptDir
$shortcut.Description = 'voice-ime 语音输入法（静默启动）'
$shortcut.WindowStyle = 7
# 图标：优先用打包好的 Release EXE，其次是 build 里生成的 icon.ico；都没有则用系统默认图标。
$iconCandidates = @(
    (Join-Path $scriptDir 'Release\MiaoYu.exe'),
    (Join-Path $scriptDir 'build\icon.ico')
)
foreach ($iconPath in $iconCandidates) {
    if (Test-Path -LiteralPath $iconPath) {
        $shortcut.IconLocation = "$iconPath,0"
        break
    }
}
$shortcut.Save()

if (-not (Test-Path -LiteralPath $shortcutPath)) {
    Write-Host "错误：快捷方式创建失败：$shortcutPath"
    exit 1
}
Write-Host "已创建开机自启快捷方式。"
exit 0
