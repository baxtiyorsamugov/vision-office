$ErrorActionPreference = "Stop"
Push-Location $PSScriptRoot
$resumeSync = $false
try {
    $running = & docker compose ps --status running --services
    if ($LASTEXITCODE -ne 0) { throw "Start Docker Desktop and the project first." }
    if ($running -notcontains "postgres") { throw "PostgreSQL is not running. Run docker compose up -d first." }
    $resumeSync = $running -contains "edge-sync"
    if ($resumeSync) {
        & docker compose stop edge-sync
        if ($LASTEXITCODE -ne 0) { throw "Could not stop edge-sync." }
    }
    $toolsPath = Join-Path $PSScriptRoot "tools"
    & docker compose run --rm --no-deps --volume "${toolsPath}:/app/tools:ro" --entrypoint python edge-sync tools/import_erp_catalog.py
    $importExit = $LASTEXITCODE
    if ($importExit -ne 0) { throw "Import did not fully complete (exit $importExit). Review the summary above." }
    Write-Host "Import complete. Refresh http://127.0.0.1:8501" -ForegroundColor Green
}
finally {
    if ($resumeSync) {
        & docker compose start edge-sync
        if ($LASTEXITCODE -ne 0) { Write-Warning "Restart edge-sync manually: docker compose start edge-sync" }
    }
    Pop-Location
}
