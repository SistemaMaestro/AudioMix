# AudioMix launcher for Windows.
# Usage (PowerShell):
#   cd "C:\Projetos GIT\AudioMix"
#   .\run.ps1
#
# Options:
#   .\run.ps1 -Install         # force reinstall/upgrade of deps
#   .\run.ps1 -Python py       # use "py -3" instead of "python"

param(
    [switch]$Install,
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $here

Write-Host "AudioMix launcher" -ForegroundColor Cyan
Write-Host "Working dir: $here"
Write-Host "Python cmd : $Python"

# Resolve python command (supports "py -3")
function Invoke-Py {
    param([string[]]$Args)
    if ($Python -eq "py") {
        & py -3 @Args
    } else {
        & $Python @Args
    }
    if ($LASTEXITCODE -ne 0) { throw "python exited with $LASTEXITCODE" }
}

# Check python exists
try {
    Invoke-Py @("--version") | Out-Null
} catch {
    Write-Host "[ERRO] Python nao encontrado. Instale em https://python.org ou rode com -Python 'py'" -ForegroundColor Red
    exit 1
}

# Install deps if missing OR if user forced
$needInstall = $Install
if (-not $needInstall) {
    try {
        Invoke-Py @("-c", "import fastapi, uvicorn, zeroconf, httpx, pydantic_settings, jinja2")
    } catch {
        $needInstall = $true
    }
}

if ($needInstall) {
    Write-Host "Instalando dependencias..." -ForegroundColor Yellow
    Invoke-Py @("-m", "pip", "install", "-r", "requirements.txt")
}

Write-Host "Subindo AudioMix.py..." -ForegroundColor Green
Invoke-Py @("AudioMix.py")
