@echo off
REM AudioMix launcher (cmd/duplo-clique).
REM Instala dependencias se faltar e sobe o servidor.

cd /d "%~dp0"

python -c "import fastapi, uvicorn, zeroconf, httpx, pydantic_settings, jinja2" 2>nul
if errorlevel 1 (
    echo Instalando dependencias...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo [ERRO] Falha ao instalar dependencias.
        pause
        exit /b 1
    )
)

python AudioMix.py
pause
