# setup_task.ps1 - Windows Task Scheduler 自动安装脚本
# 用法：右键 → 以管理员身份运行 PowerShell → 执行本脚本
#
# 创建两个定时任务：
#   1. RSI Monitor Daily Update   每日 15:30   daily_update.py（完整指标计算）
#   2. RSI Monitor Realtime Update 每 30 分钟  update_realtime.py（轻量行情更新）
#
# 注意事项：
#   - 需要管理员权限
#   - 使用当前登录用户运行（git push 需要你的 git 配置）
#   - 需要保持系统登录状态

# ============ 配置 ============
$Tasks = @(
    @{
        Name = "RSI Monitor Daily Update"
        Script = "daily_update.py"
        Desc = "每日完整更新（RSI/MA/PE 指标计算）"
        Schedule = @{Type="daily"; Time="15:30"}
    },
    @{
        Name = "RSI Monitor Realtime Update"
        Script = "update_realtime.py"
        Desc = "实时行情更新（每30分钟）"
        Schedule = @{Type="realtime"}
    }
)

$PythonPath = "python"  # 如果系统有多个 Python，可改为全路径

# ============ 前置检查 ============
$PSScriptRoot = if ($PSScriptRoot) { $PSScriptRoot } else { Get-Location }
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  RSI Monitor 定时任务安装" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

# 检查管理员权限
$IsAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole] "Administrator")
if (-not $IsAdmin) {
    Write-Host "需要管理员权限！请右键 → 以管理员身份运行 PowerShell" -ForegroundColor Red
    Read-Host "按 Enter 退出"
    exit 1
}

# 检查 Python
try {
    $PyVer = & $PythonPath --version 2>&1
    Write-Host "Python: $PyVer" -ForegroundColor Green
} catch {
    Write-Host "警告：检测不到 python 命令" -ForegroundColor Yellow
}

# ============ 创建任务函数 ============
function Create-DailyTask {
    param($TaskName, $ScriptPath, $Time)

    Write-Host "`n--- $TaskName ---"
    Write-Host "  脚本: $ScriptPath"
    Write-Host "  时间: 每日 $Time"

    if (-not (Test-Path $ScriptPath)) {
        Write-Host "  ⚠ 找不到脚本文件，跳过" -ForegroundColor Yellow
        return
    }

    # 删除旧任务
    schtasks /delete /tn "$TaskName" /f 2>$null

    # 创建
    schtasks /create /tn "$TaskName" /tr "python `"$ScriptPath`"" /sc daily /st $Time /f /it | Out-Null
    Write-Host "  ✅ 创建成功" -ForegroundColor Green
}

function Create-RealtimeTask {
    param($TaskName, $ScriptPath)

    Write-Host "`n--- $TaskName ---"
    Write-Host "  脚本: $ScriptPath"
    Write-Host "  频率: 每 30 分钟（06:00~次日06:00）"

    if (-not (Test-Path $ScriptPath)) {
        Write-Host "  ⚠ 找不到脚本文件，跳过" -ForegroundColor Yellow
        return
    }

    # 删除旧任务
    schtasks /delete /tn "$TaskName" /f 2>$null

    # 创建：每天06:00开始，每30分钟重复，持续24小时
    schtasks /create /tn "$TaskName" /tr "python `"$ScriptPath`"" /sc daily /st 06:00 /ri 30 /du 24:00 /f /it | Out-Null
    Write-Host "  ✅ 创建成功" -ForegroundColor Green
}

# ============ 执行创建 ============
foreach ($t in $Tasks) {
    $ScriptPath = Join-Path $PSScriptRoot $t.Script
    if ($t.Schedule.Type -eq "daily") {
        Create-DailyTask -TaskName $t.Name -ScriptPath $ScriptPath -Time $t.Schedule.Time
    } elseif ($t.Schedule.Type -eq "realtime") {
        Create-RealtimeTask -TaskName $t.Name -ScriptPath $ScriptPath
    }
}

# ============ 完成 ============
Write-Host "`n========================================" -ForegroundColor Cyan
Write-Host "  ✅ 全部任务安装完成！" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "📋 任务列表："
foreach ($t in $Tasks) {
    $task = Get-ScheduledTask -TaskName $t.Name -ErrorAction SilentlyContinue
    $state = if ($task -and $task.State -eq "Ready") { "✅" } else { "⏸" }
    Write-Host "  $state $($t.Name)  —  $($t.Desc)"
}
Write-Host ""
Write-Host "🔍 如需验证，打开「任务计划程序」→ 任务计划程序库" -ForegroundColor Yellow
Write-Host "🔍 如需手动测试，右键任务 → 运行" -ForegroundColor Yellow
Write-Host ""

Read-Host "按 Enter 退出"
