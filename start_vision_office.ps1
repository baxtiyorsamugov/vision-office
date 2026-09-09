param(
    [ValidateSet("auto", "cpu", "cu124", "cu128")]
    [string]$Profile = "auto",
    [ValidateRange(0, 31)][int]$GpuIndex = 0,
    [switch]$CheckOnly
)
$ErrorActionPreference = "Stop"
Push-Location $PSScriptRoot
$previousProfile = $env:VISION_OFFICE_GPU_PROFILE
$previousIndex = $env:VISION_OFFICE_GPU_INDEX
$restoreWorker = $false
$overridePath = Join-Path $PSScriptRoot "docker-compose.override.yml"
$marker = "# Managed by start_vision_office.ps1"
try {
    if (Test-Path -LiteralPath $overridePath) {
        if (-not ([IO.File]::ReadAllText($overridePath).StartsWith($marker))) {
            throw "An existing custom docker-compose.override.yml must be reviewed before automatic selection."
        }
    }
    $osType = & docker info --format '{{.OSType}}'
    if ($LASTEXITCODE -ne 0 -or $osType -ne 'linux') { throw "Start Docker Desktop (Linux containers / WSL2) first." }
    $selected = $Profile
    if ($selected -eq "auto") {
        $selected = "cpu"
        $smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
        if ($smi) {
            $gpus = @()
            try { $gpus = @(& $smi.Source --query-gpu=compute_cap --format=csv,noheader 2>$null) }
            catch { Write-Warning "NVIDIA capability query failed; using CPU candidate." }
            if ($LASTEXITCODE -eq 0 -and $gpus.Count -gt $GpuIndex) {
                $capability = 0.0
                if ([double]::TryParse($gpus[$GpuIndex].Trim(), [Globalization.NumberStyles]::Float, [Globalization.CultureInfo]::InvariantCulture, [ref]$capability)) {
                    if ($capability -ge 10) { $selected = "cu128" }
                    elseif ($capability -ge 6) { $selected = "cu124" }
                }
            }
        }
    }
    Write-Host "Candidate runtime: $selected. Building CPU recovery images first." -ForegroundColor Cyan
    & docker compose -f docker-compose.yml build
    if ($LASTEXITCODE -ne 0) { throw "CPU image build failed. Existing runtime selection unchanged." }
    if ($selected -ne "cpu") {
        $env:VISION_OFFICE_GPU_PROFILE = $selected
        $env:VISION_OFFICE_GPU_INDEX = "$GpuIndex"
        & docker compose -f docker-compose.yml -f docker-compose.gpu.yml build vision-worker
        if ($LASTEXITCODE -ne 0) {
            if ($Profile -ne "auto") { throw "Requested GPU image build failed." }
            Write-Warning "GPU build failed; trying CPU."
            $selected = "cpu"
        }
    }
    # Release existing camera contexts before testing all cameras concurrently.
    $running = @(& docker compose ps --status running --services)
    if ($LASTEXITCODE -ne 0) { throw "Cannot inspect running services." }
    $restoreWorker = $running -contains "vision-worker"
    if ($restoreWorker) {
        & docker compose stop vision-worker
        if ($LASTEXITCODE -ne 0) { throw "Could not stop the camera worker for preflight." }
    }
    if ($selected -ne "cpu") {
        & docker compose -f docker-compose.yml -f docker-compose.gpu.yml run --rm --no-deps --entrypoint python vision-worker docker/check_runtime.py --device cuda
        if ($LASTEXITCODE -ne 0) {
            if ($Profile -ne "auto") { throw "Requested GPU failed real-model preflight. Previous runtime preserved." }
            Write-Warning "CUDA unavailable, incompatible or insufficient VRAM. Checking CPU fallback."
            $selected = "cpu"
        }
    }
    if ($selected -eq "cpu") {
        & docker compose -f docker-compose.yml run --rm --no-deps --entrypoint python vision-worker docker/check_runtime.py --device cpu
        if ($LASTEXITCODE -ne 0) { throw "CPU preflight also failed. Check models and memory. No profile was changed." }
    }
    if ($CheckOnly) {
        Write-Host "Verified runtime: $selected. Check only; configuration unchanged." -ForegroundColor Green
    }
    else {
        $body = "services: {}`n"
        if ($selected -ne "cpu") {
            $body = [IO.File]::ReadAllText((Join-Path $PSScriptRoot "docker-compose.gpu.yml"))
            $body = $body.Replace('${VISION_OFFICE_GPU_PROFILE:-cu124}', $selected).Replace('${VISION_OFFICE_GPU_INDEX:-0}', "$GpuIndex")
        }
        $oldOverride = if (Test-Path -LiteralPath $overridePath) { [IO.File]::ReadAllText($overridePath) } else { $null }
        [IO.File]::WriteAllText($overridePath, "$marker`n$body", (New-Object Text.UTF8Encoding($false)))
        & docker compose up -d --no-build --wait --wait-timeout 240
        if ($LASTEXITCODE -ne 0) {
            if ($null -ne $oldOverride) { [IO.File]::WriteAllText($overridePath, $oldOverride, (New-Object Text.UTF8Encoding($false))) }
            else { Remove-Item -LiteralPath $overridePath }
            & docker compose up -d --no-build
            throw "Startup failed; previous profile restored. Review docker compose logs."
        }
        $restoreWorker = $false
        Write-Host "Selected runtime: $selected. Dashboard: http://127.0.0.1:8501" -ForegroundColor Green
        & docker compose ps
    }
}
finally {
    if ($restoreWorker) { & docker compose start vision-worker }
    $env:VISION_OFFICE_GPU_PROFILE = $previousProfile
    $env:VISION_OFFICE_GPU_INDEX = $previousIndex
    Pop-Location
}
