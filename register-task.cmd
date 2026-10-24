@echo off
rem 快速注册（简易版）：每天 08:30 无头运行早报
rem 完整版（错过开机自动补跑）请用 register-task.ps1
setlocal
set TASKNAME=lit-zaobao-daily
if exist "%~dp0dist\lit-zaobao.exe" (
  schtasks /Create /TN "%TASKNAME%" /SC DAILY /ST 08:30 /F ^
    /TR "\"%~dp0dist\lit-zaobao.exe\" today"
) else (
  schtasks /Create /TN "%TASKNAME%" /SC DAILY /ST 08:30 /F ^
    /TR "python \"%~dp0bot.py\" today"
)
if %errorlevel%==0 (
  echo 已注册计划任务 %TASKNAME%，每天 08:30 无头运行（不开窗口）。
  echo 删除：schtasks /Delete /TN %TASKNAME% /F
) else (
  echo 注册失败，请用 PowerShell 运行 register-task.ps1（功能更全）。
)
pause
