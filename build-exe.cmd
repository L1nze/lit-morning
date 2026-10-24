@echo off
rem 把 lit-zaobao 打包成单文件 exe（产物 dist\lit-zaobao.exe，可拷给没装 Python 的机器）
rem --noconsole = 无黑色控制台窗口；调试时把它去掉可以看到运行日志。
rem PyInstaller 只是构建期依赖，运行时依然是零第三方依赖。
setlocal
pip install pyinstaller || (echo pip 安装 pyinstaller 失败 & pause & exit /b 1)
pyinstaller --onefile --noconsole --name lit-zaobao --clean launcher.py
if %errorlevel%==0 (
  echo.
  echo 完成：dist\lit-zaobao.exe
  echo 把 exe 放到任意文件夹双击即可；config.json / output / data 会生成在 exe 旁边。
  echo 退出方式：应用窗口右上角「退出程序」按钮。
) else (
  echo 构建失败，请把上方报错发出来排查。
)
pause
