$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

if (-not (Test-Path -LiteralPath ".venv\Scripts\python.exe")) {
    Write-Host "Creating virtual environment..."
    python -m venv .venv
}

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
& $python -m pip install -r requirements.txt

if (-not (Test-Path -LiteralPath ".env")) {
    Copy-Item -LiteralPath ".env.example" -Destination ".env"
}

& $python desktop.py
