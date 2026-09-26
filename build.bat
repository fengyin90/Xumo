@echo off
REM 续墨 · Windows 打包脚本
REM 需要先装好 PyInstaller：pip install pyinstaller

echo [1/2] 清理旧产物...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo [2/2] 开始打包...
pyinstaller --noconfirm --clean ^
  --name Xumo ^
  --onefile ^
  --windowed ^
  --noconsole ^
  main.py

echo.
echo 完成。可执行文件在 dist\Xumo.exe
pause
