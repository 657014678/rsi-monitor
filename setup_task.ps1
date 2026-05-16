# setup_task.ps1 - Windows Task Scheduler 自动导入脚本
# 功能：创建每日定时任务，收盘后自动运行 daily_update.py
# 用法：右键 → 使用 PowerShell 运行（需管理员权限）
#
# 设置说明：
#   - 每日执行时间：15:30（收盘后）
#   - 运行用户：当前登录用户（git push 需要你的 git 配置）
#   - 需要用户登录才能运行（window 必须保持登录）
#   - 如果任务失败，每 10 分钟重试一次，最多重试 3 次

$TaskName = "RSI Monitor Daily Update"
$ScriptPath = Join-Path $PSScriptRoot "daily_update.py"
$PythonPath = "python"  # 如果系统有多个 Python，可改为全路径如 "C:\Users\wei\AppData\Local\Programs\Python\Python313\python.exe"

# ----- 检查文件是否存在 -----
if (-not (Test-Path $ScriptPath)) {
    Write-Host "错误：找不到 daily_update.py" -ForegroundColor Red
    Write-Host "预期路径：$ScriptPath"
    Write-Host "请把 setup_task.ps1 放到项目根目录再运行。"
    Read-Host "按 Enter 退出"
    exit 1
}

# ----- 检查管理员权限 -----
$IsAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole] "Administrator")
if (-not $IsAdmin) {
    Write-Host "需要管理员权限才能创建定时任务。" -ForegroundColor Yellow
    Write-Host "请右键 → 以管理员身份运行 PowerShell，然后再执行本脚本。"
    Read-Host "按 Enter 退出"
    exit 1
}

# ----- 检查 Python 是否可用 -----
try {
    $PyVer = & $PythonPath --version 2>&1
    Write-Host "Python 版本：$PyVer" -ForegroundColor Green
} catch {
    Write-Host "警告：检测不到 python 命令，脚本可能无法正常运行。" -ForegroundColor Yellow
    Write-Host "建议安装 Python 并确保已加入 PATH 环境变量。"
}

# ----- 删除已有的同名任务（如果存在） -----
$Existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($Existing) {
    Write-Host "发现已有同名任务，先删除..." -ForegroundColor Yellow
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

# ----- 创建触发器：每日 15:30 -----
$Trigger = New-ScheduledTaskTrigger -Daily -At "15:30"

# ----- 创建操作：执行 python daily_update.py -----
$Action = New-ScheduledTaskAction -Execute $PythonPath -Argument "`"$ScriptPath`"" -WorkingDirectory $PSScriptRoot

# ----- 设置（失败重试） -----
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries:$true `
    -DontStopIfGoingOnBatteries:$true `
    -StartWhenAvailable:$true `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 10)

# ----- 注册任务：以当前登录用户身份运行 -----
# 注意：用当前用户是为了 git push 能正常使用你已有的 git 配置
$CurrentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$Principal = New-ScheduledTaskPrincipal -UserId $CurrentUser -LogonType Interactive -RunLevel Limited

try {
    Register-ScheduledTask -TaskName $TaskName -Trigger $Trigger -Action $Action -Settings $Settings -Principal $Principal -Force
    Write-Host "任务创建成功！" -ForegroundColor Green
    Write-Host "任务名称：$TaskName" -ForegroundColor Cyan
    Write-Host "执行时间：每日 15:30" -ForegroundColor Cyan
    Write-Host "执行脚本：$ScriptPath" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "如需验证，打开「任务计划程序」→ 任务计划程序库 → 找到 [RSI Monitor Daily Update]" -ForegroundColor Yellow
    Write-Host "如需手动测试，可右键任务 → 运行" -ForegroundColor Yellow
} catch {
    Write-Host "任务创建失败：$($_.Exception.Message)" -ForegroundColor Red
    Write-Host ""
    Write-Host "备用方案（手动创建）：" -ForegroundColor Yellow
    Write-Host "  1. 打开「任务计划程序」" -ForegroundColor Yellow
    Write-Host "  2. 右侧 → 创建基本任务" -ForegroundColor Yellow
    Write-Host "  3. 名称：RSI Monitor Daily Update" -ForegroundColor Yellow
    Write-Host "  4. 触发器：每日，15:30" -ForegroundColor Yellow
    Write-Host "  5. 操作：启动程序 → python" -ForegroundColor Yellow
    Write-Host "  6. 参数：`"$ScriptPath`"" -ForegroundColor Yellow
    Write-Host "  7. 起始于：$PSScriptRoot" -ForegroundColor Yellow
}

Read-Host "按 Enter 退出"
