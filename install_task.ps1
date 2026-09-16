# ============================================================
#  install_task.ps1 —— 注册 / 卸载 Windows 计划任务（每日自动刷新行情缓存）
#  用法（在本目录下以 PowerShell 运行）：
#     powershell -ExecutionPolicy Bypass -File .\install_task.ps1            # 注册，默认每天 15:30
#     powershell -ExecutionPolicy Bypass -File .\install_task.ps1 -Time 09:05
#     powershell -ExecutionPolicy Bypass -File .\install_task.ps1 -Remove    # 卸载
#  说明：任务以当前用户身份运行，无需管理员权限；仅执行一次全量刷新后退出。
# ============================================================
param(
    [string]$TaskName = "StockRadarDailyRefresh",
    [string]$Time = "15:30",
    [string]$Python = "python",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppPy = Join-Path $ProjectDir "app.py"

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "[OK] 已卸载计划任务：$TaskName" -ForegroundColor Green
    } else {
        Write-Host "[INFO] 计划任务不存在：$TaskName" -ForegroundColor Yellow
    }
    return
}

if (-not (Test-Path $Python)) { $Python = "python" }
if (-not (Test-Path $AppPy)) { throw "未找到 app.py：$AppPy" }

$action = New-ScheduledTaskAction -Execute $Python `
    -Argument "`"$AppPy`" --refresh --force-kline" -WorkingDirectory $ProjectDir

# 每个交易日（周一至周五）指定时间执行
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $Time

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Stock Radar 每日自动刷新东方财富行情缓存（项目目录：$ProjectDir）" -Force | Out-Null

Write-Host "[OK] 计划任务已注册：$TaskName" -ForegroundColor Green
Write-Host "     执行时间：每周一至周五 $Time"
Write-Host "     执行命令：$Python `"$AppPy`" --refresh --force-kline"
Write-Host "     查看/删除：Get-ScheduledTask -TaskName $TaskName | Unregister-ScheduledTask -Confirm:`$false"
