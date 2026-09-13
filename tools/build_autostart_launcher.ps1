param([switch]$Clean)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { $python = "python" }
$source = Join-Path $root "launcher\vision_office_autostart.py"
$payload = Join-Path $root "tools\docker_autostart.ps1"
$output = Join-Path $root "launcher\VisionOfficeAutostart.exe"
$buildRoot = Join-Path $root ".build\autostart"

if (-not (Test-Path -LiteralPath $source) -or -not (Test-Path -LiteralPath $payload)) {
    throw "Autostart launcher source or PowerShell payload is missing."
}
& $python -m PyInstaller --version *> $null
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller is missing. Install build tools with: .\\.venv\\Scripts\\python.exe -m pip install -r tools\\requirements-build.txt"
}
if ($Clean -and (Test-Path -LiteralPath $buildRoot)) { Remove-Item -LiteralPath $buildRoot -Recurse -Force }
New-Item -ItemType Directory -Path $buildRoot -Force | Out-Null

& $python -m PyInstaller --noconfirm --clean --onefile --noconsole --name VisionOfficeAutostart `
    --distpath (Join-Path $buildRoot "dist") `
    --workpath (Join-Path $buildRoot "work") `
    --specpath (Join-Path $buildRoot "spec") `
    --add-data "$payload;tools" `
    $source
if ($LASTEXITCODE -ne 0) { throw "Could not build VisionOfficeAutostart.exe." }

Copy-Item (Join-Path $buildRoot "dist\VisionOfficeAutostart.exe") $output -Force
Write-Host "Built $output" -ForegroundColor Green
