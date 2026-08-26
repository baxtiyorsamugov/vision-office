$ErrorActionPreference = "Stop"

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "The application environment is missing. Run .\install_vision_office.ps1 first."
}

& $python (Join-Path $PSScriptRoot "main.py")
exit $LASTEXITCODE
