$ErrorActionPreference = "Stop"

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    $python = "python"
}

& $python (Join-Path $PSScriptRoot "tools\create_update_archive.py")
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "`nDone. Copy acs2-update.zip to the flash drive." -ForegroundColor Green
