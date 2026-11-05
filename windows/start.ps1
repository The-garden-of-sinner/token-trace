<#
.SYNOPSIS
    token-trace Windows 启动器（对应 Linux 的 start.sh）
.DESCRIPTION
    幂等启动 / 停止 / 查看状态；不依赖 curl、Git Bash 或 WSL。
    默认用 python.exe + -WindowStyle Hidden 启动，而不是 pythonw.exe：
    pythonw 没有控制台，sys.stdout 为 None，trace_server.main() 里的
    print() 会直接抛 AttributeError。
.PARAMETER Action
    start（默认）| stop | restart | status
.PARAMETER Port
    服务端口，默认 8765；也可用环境变量 TRACE_PORT
.PARAMETER NoBrowser
    只起服务不开浏览器（无头 / 自动化）
.PARAMETER WorkBuddyHome
    覆盖数据目录（需仓库先实现 data_dir() 读 WORKBUDDY_HOME）
.PARAMETER Token
    访问令牌，也可由环境变量 TRACE_TOKEN 提供。设置后启动参数带 --token，
    所有请求自动加 Authorization: Bearer，浏览器首访用 ?token= 种 Cookie。
    别名 ShutdownToken 保留兼容。
.EXAMPLE
    .\start.ps1
    .\start.ps1 stop
    .\start.ps1 -Port 9000 -NoBrowser
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('start', 'stop', 'restart', 'status')]
    [string]$Action = 'start',
    [int]$Port = $(if ($env:TRACE_PORT) { [int]$env:TRACE_PORT } else { 8765 }),
    [switch]$NoBrowser,
    [string]$WorkBuddyHome,
    [Alias('ShutdownToken')]
    [string]$Token
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
if (-not $Token -and $env:TRACE_TOKEN) { $Token = $env:TRACE_TOKEN }

# Windows 控制台默认 GBK，强制 Python 走 UTF-8，避免中文输出乱码
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
if ($WorkBuddyHome) { $env:WORKBUDDY_HOME = $WorkBuddyHome }

$ScriptDir = $PSScriptRoot
# 允许放在仓库根，也允许放在 windows/ 子目录
$Server = Join-Path $ScriptDir 'trace_server.py'
if (-not (Test-Path -LiteralPath $Server)) {
    $Server = Join-Path (Split-Path -Parent $ScriptDir) 'trace_server.py'
}
if (-not (Test-Path -LiteralPath $Server)) {
    throw "找不到 trace_server.py（已尝试 $ScriptDir 及其上级目录）"
}

$Base = "http://127.0.0.1:$Port"

# --- 用 HttpWebRequest 而不是 Invoke-WebRequest：绕过系统代理、不依赖 IE 引擎，
#     PowerShell 5.1 / 7 都能跑，且不会因 localhost 走代理而误判。
function Invoke-Local {
    param(
        [string]$Uri,
        [string]$Method = 'GET',
        [int]$TimeoutMs = 1000,
        [hashtable]$Headers = @{}
    )
    $req = [System.Net.HttpWebRequest]::Create($Uri)
    $req.Method = $Method
    $req.Timeout = $TimeoutMs
    $req.ReadWriteTimeout = $TimeoutMs
    $req.Proxy = $null
    if ($Token -and -not $Headers.ContainsKey('Authorization')) {
        $req.Headers.Add('Authorization', "Bearer $Token")
    }
    foreach ($k in $Headers.Keys) { $req.Headers.Add($k, $Headers[$k]) }
    try {
        $resp = $req.GetResponse()
        $code = [int]$resp.StatusCode
        $sr = New-Object System.IO.StreamReader($resp.GetResponseStream(), [System.Text.Encoding]::UTF8)
        $body = $sr.ReadToEnd()
        $sr.Close(); $resp.Close()
        return @{ Code = $code; Body = $body }
    } catch [System.Net.WebException] {
        if ($_.Exception.Response) {
            return @{ Code = [int]$_.Exception.Response.StatusCode; Body = '' }
        }
        return $null
    } catch {
        return $null
    }
}

function Test-Service {
    $r = Invoke-Local -Uri "$Base/health" -TimeoutMs 1000
    # 不只认 200，还认 body == "ok"，避免端口被别的服务占用时误判
    return ($null -ne $r -and $r.Code -eq 200 -and $r.Body.Trim() -eq 'ok')
}

function Resolve-Python {
    foreach ($n in @('py', 'python', 'python3')) {
        $c = Get-Command $n -ErrorAction SilentlyContinue
        if (-not $c) { continue }
        $p = $c.Source
        if ((Split-Path -Leaf $p) -ieq 'py.exe') {
            try {
                $real = (& $p -3 -c "import sys; print(sys.executable)" 2>$null | Select-Object -First 1)
                if ($real -and (Test-Path -LiteralPath $real.Trim())) { return $real.Trim() }
            } catch { }
            continue
        }
        return $p
    }
    throw '找不到 Python 3。请安装 Python 3.8+ 并勾选 Add to PATH，或安装 py 启动器。'
}

function Stop-Service {
    $r = Invoke-Local -Uri "$Base/shutdown" -Method 'POST' -TimeoutMs 2000
    if ($null -ne $r -and $r.Code -eq 200) {
        Write-Host "Token 轨迹服务已停止（$Base）"
        return
    }
    # 兜底：老实例没有 /shutdown 端点时按命令行精确匹配 PID
    $procs = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
             Where-Object { $_.CommandLine -like "*trace_server.py*" -and $_.CommandLine -like "*--port $Port*" }
    if ($procs) {
        $procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Write-Host "Token 轨迹服务已停止（按进程匹配，端口 $Port）"
    } else {
        Write-Host "没有正在运行的 Token 轨迹服务（端口 $Port）"
    }
}

function Start-Service {
    if (Test-Service) {
        Write-Host "服务已在运行，直接打开：$Base"
    } else {
        $Py = Resolve-Python
        Write-Host "正在启动 Token 轨迹服务…（$Py）"
        # Start-Process -ArgumentList 会把数组用空格拼成一行，
        # 含中文/空格的脚本路径必须自己加双引号，否则会被拆成多个参数。
        $argList = @('-B', ('"{0}"' -f $Server), '--port', $Port)
        if ($Token) { $argList += @('--token', ('"{0}"' -f $Token)) }
        Start-Process -FilePath $Py -ArgumentList $argList -WindowStyle Hidden | Out-Null
        for ($i = 0; $i -lt 40; $i++) {
            if (Test-Service) { break }
            Start-Sleep -Milliseconds 250
        }
        if (-not (Test-Service)) {
            Write-Error "启动失败。请手动执行一次查看报错：`n  `"$Py`" -B `"$Server`" --port $Port"
            exit 1
        }
        Write-Host "服务已就绪：$Base"
    }
    if (-not $NoBrowser) {
        $openUrl = "$Base/"
        if ($Token) { $openUrl = "$Base/?token=" + [uri]::EscapeDataString($Token) }
        Start-Process $openUrl
    }
}

switch ($Action) {
    'start'   { Start-Service }
    'stop'    { Stop-Service }
    'restart' { Stop-Service; Start-Sleep -Milliseconds 400; Start-Service }
    'status'  {
        if (Test-Service) {
            $r = Invoke-Local -Uri "$Base/api/health" -TimeoutMs 2000
            Write-Host "运行中：$Base"
            if ($r) { Write-Host $r.Body }
        } else {
            Write-Host "未运行：$Base"
        }
    }
}
