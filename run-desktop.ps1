$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

if (-not (Test-Path -LiteralPath ".venv\Scripts\python.exe")) {
    Write-Host "Creating virtual environment..."
    python -m venv .venv
}

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$requirements = Join-Path $PSScriptRoot "requirements.txt"
$dependencyStamp = Join-Path $PSScriptRoot ".venv\.requirements.sha256"
$requirementsHash = (Get-FileHash -LiteralPath $requirements -Algorithm SHA256).Hash
$installedHash = if (Test-Path -LiteralPath $dependencyStamp) {
    (Get-Content -Raw -LiteralPath $dependencyStamp).Trim()
} else {
    ""
}

if ($installedHash -ne $requirementsHash) {
    Write-Host "Installing application dependencies..."
    & $python -m pip install -r $requirements
    if ($LASTEXITCODE -ne 0) {
        throw "Dependency installation failed with exit code $LASTEXITCODE"
    }
    Set-Content -LiteralPath $dependencyStamp -Value $requirementsHash -Encoding ASCII
}

if (-not (Test-Path -LiteralPath ".env")) {
    Copy-Item -LiteralPath ".env.example" -Destination ".env"
}

& $python desktop.py
