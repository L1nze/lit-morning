# 注册 Windows 计划任务：每天 08:30 无头运行早报（exe 模式，不开窗口）
# 右键「以 PowerShell 运行」或：powershell -File register-task.ps1
# 错过开机时间会补跑（StartWhenAvailable），电源允许时唤醒执行。

$TaskName = "lit-zaobao-daily"
$Exe = Join-Path $PSScriptRoot "dist\lit-zaobao.exe"
if (-not (Test-Path $Exe)) {
    # 没构建 exe 时退回 python bot.py
    $Py = "python"
    $BotPy = Join-Path $PSScriptRoot "bot.py"
    $Action = New-ScheduledTaskAction -Execute $Py -Argument "`"$BotPy`" today" -WorkingDirectory $PSScriptRoot
    Write-Host "未找到 dist\lit-zaobao.exe，使用 python bot.py 模式"
} else {
    $Action = New-ScheduledTaskAction -Execute $Exe -Argument "today" -WorkingDirectory (Split-Path $Exe)
    Write-Host "使用 exe 无头模式: $Exe today"
}

$Trigger  = New-ScheduledTaskTrigger -Daily -At 08:30
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 15)
# StartWhenAvailable = 电脑当时没开机 → 开机后尽快补跑

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Force | Out-Null
Write-Host "已注册计划任务 [$TaskName]：每天 08:30 运行（错过自动补跑）"
Write-Host "查看: taskschd.msc ；删除: Unregister-ScheduledTask -TaskName $TaskName -Confirm:`$false"
Write-Host "立刻试跑: Start-ScheduledTask -TaskName $TaskName"
