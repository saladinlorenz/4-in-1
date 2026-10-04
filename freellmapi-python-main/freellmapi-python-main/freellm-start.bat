@echo off
REM Lanceur FreeLLMAPI — double-clic pour démarrer avec tray + dashboard
REM Arrière-plan sans console : utilise pythonw si disponible
setlocal
cd /d "%~dp0"
where pythonw >nul 2>&1
if %errorlevel%==0 (
    echo Lancement en arriere-plan (tray)...
    start "" pythonw -m freellm --tray
    timeout /t 2 >nul
    start http://localhost:3001
) else (
    python -m freellm --tray
)
