@echo off
REM FreeLLMAPI — Lanceur verifie avant de lancer
setlocal EnableDelayedExpansion
cd /d "%~dp0"
chcp 65001 >nul 2>&1
echo ==========================================
echo  FreeLLMAPI - Verifications avant lancement
echo ==========================================
echo.

:: 1. Python version
echo [1/7] Verification Python...
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERREUR] Python introuvable. Installez Python 3.10+ depuis https://python.org (cochez "Add to PATH")
    pause
    exit /b 1
)
for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo [OK] Python !PYVER!

:: 2. Pip
echo [2/7] Verification pip...
python -m pip --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERREUR] pip indisponible (python -m ensurepip)
    pause
    exit /b 1
)
echo [OK] pip

:: 3. Port libre ?
echo [3/7] Verification port 3001...
netstat -ano | findstr ":3001" | findstr "LISTENING" >nul 2>&1
if %errorlevel% equ 0 (
    echo [WARN] Port 3001 deja utilise (serveur deja lance ?)
    echo       Ouvrez http://localhost:3001 directement ou faites freellm-stop.bat
    timeout /t 2 >nul
    start "" "http://localhost:3001"
    exit /b 0
)
echo [OK] Port libre

:: 4. Dossier data + DB inscriptible
echo [4/7] Verification dossier data...
if not exist "data" mkdir "data" 2>nul
if not exist "data\logs" mkdir "data\logs" 2>nul
echo test > "data\.writetest" 2>nul
if %errorlevel% neq 0 (
    echo [ERREUR] Dossier data non inscriptible
    pause
    exit /b 1
)
del "data\.writetest" 2>nul
echo [OK] data/

:: 5. Dependances
echo [5/7] Verification dependances...
python -c "import fastapi, uvicorn, httpx, cryptography" 2>nul
if %errorlevel% neq 0 (
    echo [INFO] Installation requirements.txt...
    python -m pip install -r requirements.txt -q
    if %errorlevel% neq 0 (
        echo [WARN] Echec silencieux, reessai verbeux...
        python -m pip install -r requirements.txt
        if %errorlevel% neq 0 (
            echo [ERREUR] Installation echouee
            pause
            exit /b 1
        )
    )
    echo [OK] requirements installes
) else (
    echo [OK] fastapi/uvicorn/httpx/cryptography presents
)
python -c "import pystray, PIL" 2>nul
if %errorlevel% neq 0 (
    echo [INFO] Installation pystray Pillow (tray)...
    python -m pip install pystray Pillow -q 2>nul
    if %errorlevel% neq 0 ( echo [WARN] Tray optionnel non installe ) else ( echo [OK] tray )
) else (
    echo [OK] pystray/Pillow presents
)

:: 6. Module freellm importable
echo [6/7] Verification module freellm...
python -c "import freellm" 2>nul
if %errorlevel% neq 0 (
    echo [INFO] Installation freellm en mode developpement...
    python -m pip install -e . -q
    if %errorlevel% neq 0 (
        echo [ERREUR] pip install -e . echoue
        pause
        exit /b 1
    )
    echo [OK] freellm installe
) else (
    echo [OK] freellm importable
)

:: 7. .env et cle
echo [7/7] Verification .env...
if not exist ".env" (
    echo [INFO] Creation .env...
    python -c "import secrets; print('ENCRYPTION_KEY=' + secrets.token_hex(32))" > .env
    echo PORT=3001 >> .env
    echo HOST=0.0.0.0 >> .env
    echo FREEAPI_DB_PATH=./data/freeapi.db >> .env
    echo [OK] .env cree
) else (
    findstr /C:"ENCRYPTION_KEY" .env >nul 2>&1
    if %errorlevel% neq 0 (
        echo [WARN] ENCRYPTION_KEY manquant dans .env, ajout...
        python -c "import secrets; print('ENCRYPTION_KEY=' + secrets.token_hex(32))" >> .env
    )
    echo [OK] .env present
)

echo.
echo [OK] Toutes verifications passees — lancement...
echo.

:: Lancement
where pythonw >nul 2>&1
if %errorlevel%==0 (
    start "" pythonw -m freellm --tray
) else (
    start "" python -m freellm --tray
)

:: Attente serveur pret (poll /livez)
echo [INFO] Attente serveur (max 20s)...
for /l %%i in (1,1,40) do (
    curl -s http://localhost:3001/livez >nul 2>&1
    if !errorlevel! equ 0 (
        echo [OK] Serveur pret en %%i s
        goto :open_browser
    )
    timeout /t 1 >nul
)
echo [WARN] Serveur pas repondu apres 20s — ouverture quand meme
:open_browser
start "" "http://localhost:3001"
echo.
echo ==========================================
echo  FreeLLMAPI lance ! Dashboard: http://localhost:3001
echo  Icone dans les icones cachees (fleche en bas droite)
echo  Fermer cette fenetre est sans risque (serveur en arriere-plan)
echo ==========================================
timeout /t 3 >nul
exit /b 0
