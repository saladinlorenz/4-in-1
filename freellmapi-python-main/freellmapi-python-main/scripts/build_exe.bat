@echo off
REM Build .exe FreeLLMAPI (Windows) — double-clic ou lancer depuis cmd
setlocal
cd /d "%~dp0.."
echo [build] installation deps...
pip install -r requirements.txt
pip install pyinstaller pillow pystray -q
echo [build] PyInstaller...
python scripts\build_exe.py
echo.
echo [build] termine — voir dist\FreeLLMAPI.exe
pause
