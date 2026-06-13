$ErrorActionPreference = "Stop"

Set-Location -LiteralPath $PSScriptRoot

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)]
        [scriptblock]$Command,
        [Parameter(Mandatory = $true)]
        [string]$FailureMessage
    )

    & $Command
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host $FailureMessage -ForegroundColor Red
        exit $LASTEXITCODE
    }
}

Write-Host "==================================================" -ForegroundColor Green
Write-Host " Caobuhu Agri Monitor - One Click Environment Setup " -ForegroundColor Green
Write-Host "==================================================" -ForegroundColor Green
Write-Host ""

try {
    $pythonVersion = & python --version
    Write-Host "Detected Python: $pythonVersion" -ForegroundColor Cyan
}
catch {
    Write-Host "Python was not found." -ForegroundColor Red
    Write-Host "Please install Python 3.10-3.12 and check 'Add Python to PATH'." -ForegroundColor Yellow
    exit 1
}

$venvPath = Join-Path $PSScriptRoot ".venv"
$venvPython = Join-Path $venvPath "Scripts\python.exe"

if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host "Creating virtual environment..." -ForegroundColor Cyan
    Invoke-Checked { python -m venv $venvPath } "Failed to create virtual environment."
}
else {
    Write-Host "Virtual environment already exists." -ForegroundColor Cyan
}

Write-Host "Upgrading pip tooling..." -ForegroundColor Cyan
Invoke-Checked { & $venvPython -m pip install --upgrade pip setuptools wheel -i https://pypi.tuna.tsinghua.edu.cn/simple } "Failed to upgrade pip tooling."

Write-Host "Installing all dependencies from requirements.txt..." -ForegroundColor Cyan
Invoke-Checked { & $venvPython -m pip install -r (Join-Path $PSScriptRoot "requirements.txt") -i https://pypi.tuna.tsinghua.edu.cn/simple } "Failed to install dependencies."

Write-Host "Creating local data folders..." -ForegroundColor Cyan
$folders = @(
    "data\S2\DANGYANG\raw_zip",
    "data\S2\DANGYANG\raw_safe",
    "data\S2\DANGYANG\processed",
    "data\S1\DANGYANG\raw_zip",
    "data\S1\DANGYANG\raw_safe",
    "data\ROI",
    "download"
)
foreach ($folder in $folders) {
    New-Item -ItemType Directory -Force -Path (Join-Path $PSScriptRoot $folder) | Out-Null
}

$envPath = Join-Path $PSScriptRoot ".env"
if (-not (Test-Path -LiteralPath $envPath)) {
    Write-Host "Creating .env template..." -ForegroundColor Cyan
    @"
# Copernicus Data Space account.
# Fill these two values if you need data download.
CDSE_USERNAME=
CDSE_PASSWORD=

# Optional defaults.
CDSE_MAX_WORKERS=2

# Earthdata / ASF account for Sentinel-1 download.
EARTHDATA_USERNAME=
EARTHDATA_PASSWORD=
EARTHDATA_TOKEN=
"@ | Set-Content -Path $envPath -Encoding UTF8
}

Write-Host "Verifying core imports..." -ForegroundColor Cyan
Invoke-Checked { & $venvPython -c "import flask, rasterio, numpy, PIL, requests, dotenv, rich, tenacity, shapefile, asf_search, tqdm; print('core imports ok')" } "Core import verification failed."

Write-Host ""
Write-Host "==================================================" -ForegroundColor Green
Write-Host " Setup complete " -ForegroundColor Green
Write-Host "==================================================" -ForegroundColor Green
Write-Host ""
Write-Host "If you need data download, edit this file first:" -ForegroundColor Yellow
Write-Host "  .env" -ForegroundColor White
Write-Host "Then start the system with the one-click start batch file in this folder." -ForegroundColor Yellow
