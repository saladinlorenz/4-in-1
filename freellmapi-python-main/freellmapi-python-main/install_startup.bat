@echo off
REM Installe FreeLLMAPI au démarrage Windows (dossier Startup)
setlocal
set SRC=%~dp0freellm-start.bat
set DST=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\FreeLLMAPI.bat
copy /Y "%SRC%" "%DST%"
echo Installe dans: %DST%
echo FreeLLMAPI se lancera au demarrage.
pause
