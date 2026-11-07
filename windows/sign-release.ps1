<#
.SYNOPSIS
    可选：用你自己的代码签名证书给仓库里的 PowerShell 脚本做 Authenticode 签名。
.DESCRIPTION
    Windows Authenticode 适用于 PE 文件（.exe / .dll / .msi）、.ps1、.cat 等，
    不适用于 .bat —— 批处理是纯文本脚本，没有可签名的 PE 结构，
    给它加图标也只能靠快捷方式（本项目已内嵌 TokenTrace.ico 并由
    TokenTrace.bat shortcut 创建带图标的快捷方式）。
    因此本仓库用 Release 里的 SHA256SUMS 覆盖发布产物的完整性校验，
    本脚本只供维护者给 windows/*.ps1 签名。仓库不附带任何证书或私钥：
    没有代码签名证书时无需运行，直接分发即可。

    需要真正的 Authenticode 签名 + 原生图标，必须另行产出 .exe / .msix
    载体并购买 OV / EV 代码签名证书，见 README「已知限制」。
.EXAMPLE
    .\sign-release.ps1 -PfxPath C:\certs\code-signing.pfx
.EXAMPLE
    .\sign-release.ps1 -Thumbprint 0123456789ABCDEF0123456789ABCDEF01234567
.NOTES
    时间戳服务器需要联网；用 RFC 3161 时间戳可让签名在证书过期后依然有效。
#>
[CmdletBinding()]
param(
    [string]$PfxPath,
    [SecureString]$PfxPassword,
    [string]$Thumbprint,
    [string]$TimestampServer = 'http://timestamp.digicert.com'
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

# 1. 取证书：PFX 文件 > 指定指纹 > 当前用户证书存储里的第一个代码签名证书
$cert = $null
if ($PfxPath) {
    if (-not (Test-Path -LiteralPath $PfxPath)) { throw "找不到证书文件：$PfxPath" }
    if ($PfxPassword) {
        $cert = Get-PfxCertificate -FilePath $PfxPath -Password $PfxPassword
    } else {
        $cert = Get-PfxCertificate -FilePath $PfxPath
    }
} elseif ($Thumbprint) {
    $cert = Get-ChildItem -Path "Cert:\CurrentUser\My\$Thumbprint" -CodeSigningCert -ErrorAction SilentlyContinue
} else {
    $cert = Get-ChildItem -Path 'Cert:\CurrentUser\My' -CodeSigningCert -ErrorAction SilentlyContinue |
            Select-Object -First 1
}
if (-not $cert) {
    throw '未找到代码签名证书。请传入 -PfxPath / -Thumbprint，或把证书导入 Cert:\CurrentUser\My。'
}
Write-Host "证书: $($cert.Subject)  指纹 $($cert.Thumbprint)"

# 2. 签仓库里的 PowerShell 脚本（排除本脚本自身即可，签它也没有副作用）
$root = Split-Path -Parent $PSScriptRoot
$scripts = Get-ChildItem -LiteralPath (Join-Path $root 'windows') -Filter '*.ps1' -File
foreach ($s in $scripts) {
    $r = Set-AuthenticodeSignature -FilePath $s.FullName -Certificate $cert `
            -HashAlgorithm SHA256 -TimestampServer $TimestampServer
    Write-Host ('{0,-26} {1}' -f $s.Name, $r.Status)
    if ($r.Status -ne 'Valid') { Write-Warning $r.StatusMessage }
}
Write-Host ''
Write-Host '发布产物（.deb / .bat）请通过 SHA256SUMS 校验，脚本无法给它们做 Authenticode 签名。'
