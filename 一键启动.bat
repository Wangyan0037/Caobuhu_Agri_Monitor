@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"

if not exist "%PYTHON_EXE%" (
    echo Virtual environment Python was not found:
    echo %PYTHON_EXE%
    echo.
    echo Please run install_env.ps1 or the environment setup BAT first.
    pause
    exit /b 1
)

echo Releasing port 5078 before starting this system...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ports = Get-NetTCPConnection -LocalPort 5078 -State Listen -ErrorAction SilentlyContinue; foreach ($conn in $ports) { $opid = $conn.OwningProcess; $proc = Get-Process -Id $opid -ErrorAction SilentlyContinue; if ($proc) { Write-Host ('Stopping process on port 5078: PID=' + $proc.Id + ' ' + $proc.ProcessName); Stop-Process -Id $opid -Force } }"

"%PYTHON_EXE%" "%~dp0main.py"

if errorlevel 1 (
    echo.
    echo Program exited with an error.
    pause
)
