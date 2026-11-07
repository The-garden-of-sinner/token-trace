<#
.SYNOPSIS
    为 token-trace 创建 Windows 桌面 / 开始菜单快捷方式（对应 install-desktop.sh）
.DESCRIPTION
    快捷方式直接指向 powershell.exe 并带 -WindowStyle Hidden，
    双击时不会出现黑色控制台窗口，直接起服务并打开浏览器。
    不写死用户名或安装路径，移动仓库后重跑一次即可。
.EXAMPLE
    .\install-shortcut.ps1
    .\install-shortcut.ps1 -Remove
#>
[CmdletBinding()]
param([switch]$Remove)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$ScriptDir = $PSScriptRoot
$Launcher = Join-Path $ScriptDir 'start.ps1'
if (-not (Test-Path -LiteralPath $Launcher)) { throw "找不到 $Launcher" }

$Name = 'Token 轨迹.lnk'
$PSExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
if (-not (Test-Path -LiteralPath $PSExe)) { $PSExe = (Get-Command powershell.exe).Source }

# -WindowStyle Hidden 必须是 powershell.exe 自己的参数，放在 -File 之前
$Arguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $Launcher
$Description = '一键启动本地 Token 轨迹服务并在浏览器打开'
# 优先用项目自带图标（仓库根 assets/TokenTrace.ico），缺失时退回系统图标
$RepoIcon = Join-Path (Split-Path -Parent $ScriptDir) 'assets\TokenTrace.ico'
if (Test-Path -LiteralPath $RepoIcon) {
    $Icon = $RepoIcon
    $IconIndex = 0
} else {
    $Icon = Join-Path $env:SystemRoot 'System32\shell32.dll'
    $IconIndex = 21
}

$Desktop = [Environment]::GetFolderPath('Desktop')
$Programs = Join-Path ([Environment]::GetFolderPath('Programs')) 'Token Trace'
$Targets = @((Join-Path $Desktop $Name), (Join-Path $Programs $Name))

if ($Remove) {
    foreach ($p in $Targets) {
        if (Test-Path -LiteralPath $p) { Remove-Item -LiteralPath $p -Force; Write-Host "已删除: $p" }
    }
    return
}

if (-not (Test-Path -LiteralPath $Programs)) { New-Item -ItemType Directory -Path $Programs -Force | Out-Null }

$sh = New-Object -ComObject WScript.Shell
foreach ($p in $Targets) {
    $lnk = $sh.CreateShortcut($p)
    $lnk.TargetPath = $PSExe
    $lnk.Arguments = $Arguments
    $lnk.WorkingDirectory = $ScriptDir
    $lnk.Description = $Description
    if (Test-Path -LiteralPath $Icon) { $lnk.IconLocation = "$Icon,$IconIndex" }
    $lnk.Save()
    Write-Host "快捷方式: $p"
}
Write-Host "启动器 = $Launcher"
Write-Host "提示：路径含中文或空格时，本文件需以 UTF-8 BOM 保存（PowerShell 5.1 按 ANSI 读取无 BOM 的 .ps1）。"
