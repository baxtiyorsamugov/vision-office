# Isolated orchestration tests; Docker and NVIDIA commands are stubbed.
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$fixture = Join-Path ([IO.Path]::GetTempPath()) ("vision-runtime-test-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $fixture | Out-Null
Copy-Item (Join-Path $root 'start_vision_office.ps1') $fixture
Copy-Item (Join-Path $root 'docker-compose.gpu.yml') $fixture
$global:runtimeTestCalls = [Collections.Generic.List[string]]::new()
$global:runtimeTestGpuFailed = $true
$global:runtimeTestCapability = '8.6'
$global:runtimeTestStartupFailed = $false
function Get-Command { if ($global:runtimeTestCapability) { return @{Source = 'FakeNvidia'} } }
function FakeNvidia { $global:LASTEXITCODE = 0; $global:runtimeTestCapability }
function docker {
    $line = $args -join ' '
    $global:runtimeTestCalls.Add($line)
    $global:LASTEXITCODE = 0
    if ($line -eq 'info --format {{.OSType}}') { return 'linux' }
    if ($line -match 'ps --status running') { return @('postgres', 'vision-worker') }
    if ($line -match '--device cuda' -and $global:runtimeTestGpuFailed) { $global:LASTEXITCODE = 1 }
    if ($line -match 'up .*--wait' -and $global:runtimeTestStartupFailed) { $global:LASTEXITCODE = 1 }
}
try {
    & (Join-Path $fixture 'start_vision_office.ps1') -CheckOnly
    if (-not ($global:runtimeTestCalls | Where-Object { $_ -match '--device cpu' })) { throw 'CPU fallback was not checked' }
    if (-not ($global:runtimeTestCalls -contains 'compose start vision-worker')) { throw 'Stopped camera was not restored' }
    if (Test-Path (Join-Path $fixture 'docker-compose.override.yml')) { throw 'CheckOnly wrote an override' }
    $global:runtimeTestCalls.Clear()
    $failed = $false
    try { & (Join-Path $fixture 'start_vision_office.ps1') -Profile cu124 -CheckOnly } catch { $failed = $true }
    if (-not $failed) { throw 'Explicit GPU failure must fail' }
    if ($global:runtimeTestCalls | Where-Object { $_ -match '--device cpu' }) { throw 'Explicit GPU must not silently change profile' }
    $global:runtimeTestCalls.Clear()
    $global:runtimeTestGpuFailed = $false
    & (Join-Path $fixture 'start_vision_office.ps1') -Profile cu124
    $saved = [IO.File]::ReadAllText((Join-Path $fixture 'docker-compose.override.yml'))
    if ($saved -notmatch 'vision-office-gpu:cu124' -or $saved -match '\$\{') { throw 'GPU selection was not persisted literally' }
    & (Join-Path $fixture 'start_vision_office.ps1') -Profile cpu
    $saved = [IO.File]::ReadAllText((Join-Path $fixture 'docker-compose.override.yml'))
    if ($saved -match 'nvidia|gpu:') { throw 'CPU override still reserves GPU' }
    foreach ($case in @(@('', 'cpu'), @('5.2', 'cpu'), @('6.1', 'cu124'), @('8.9', 'cu124'), @('12.0', 'cu128'))) {
        $global:runtimeTestCapability = $case[0]
        $global:runtimeTestCalls.Clear()
        & (Join-Path $fixture 'start_vision_office.ps1')
        $body = [IO.File]::ReadAllText((Join-Path $fixture 'docker-compose.override.yml'))
        if ($case[1] -eq 'cpu') {
            if ($body -match 'nvidia|gpu:') { throw "Wrong GPU selection for $($case[0])" }
        } elseif ($body -notmatch "vision-office-gpu:$($case[1])") { throw "Wrong profile for $($case[0])" }
    }
    $oldBody = [IO.File]::ReadAllText((Join-Path $fixture 'docker-compose.override.yml'))
    $global:runtimeTestStartupFailed = $true
    $failed = $false
    try { & (Join-Path $fixture 'start_vision_office.ps1') -Profile cpu } catch { $failed = $true }
    if (-not $failed -or [IO.File]::ReadAllText((Join-Path $fixture 'docker-compose.override.yml')) -ne $oldBody) { throw 'Startup failure must restore previous selection' }
    Write-Output 'PASS: auto fallback, check-only restoration, explicit failure, profile persistence, no-GPU/Pascal/Ada/Blackwell selection, startup rollback'
}
finally {
    $resolved = [IO.Path]::GetFullPath($fixture)
    if (-not $resolved.StartsWith([IO.Path]::GetFullPath([IO.Path]::GetTempPath())) -or (Split-Path $resolved -Leaf) -notlike 'vision-runtime-test-*') { throw 'Unsafe temporary path' }
    Remove-Item -LiteralPath $resolved -Recurse -Force
    Remove-Variable runtimeTestCalls, runtimeTestGpuFailed, runtimeTestCapability, runtimeTestStartupFailed -Scope Global
}
