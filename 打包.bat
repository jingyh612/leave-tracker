@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo 正在准备打包工具...
python -m pip install --disable-pip-version-check pyinstaller
if errorlevel 1 (
  echo 没找到 Python，这台电脑需要先安装 Python 才能打包。别人拿到程序后不用装。
  pause
  exit /b 1
)

echo.
echo 正在打包，请稍等...
python -m PyInstaller --noconfirm --clean --windowed --name 离开记录器 leave_tracker.py
if errorlevel 1 (
  echo 打包失败。
  pause
  exit /b 1
)

copy /y "%~dp0使用说明.txt" "%~dp0dist\离开记录器\使用说明.txt" >nul

echo.
echo 打包完成：
echo %~dp0dist\离开记录器
echo.
echo 把 dist\离开记录器 整个文件夹打成 zip 发给别人。
echo 对方解压后双击「离开记录器.exe」即可，不用安装 Python。
pause
