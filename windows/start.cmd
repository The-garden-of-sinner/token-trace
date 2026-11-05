@echo off
REM token-trace 双击入口：转调 PowerShell 启动器，绕过默认执行策略
chcp 65001 >nul 2>&1
where pwsh >nul 2>&1
if %errorlevel%==0 (
    pwsh.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
) else (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
)
