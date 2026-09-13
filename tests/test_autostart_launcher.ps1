# Isolated tests for the Windows sign-in helper. No real Docker Desktop is used.
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$fixture = Join-Path ([IO.Path]::GetTempPath()) ("vision-autostart-test-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $fixture | Out-Null
New-Item -ItemType Directory -Path (Join-Path $fixture "data\logs") -Force | Out-Null
Copy-Item (Join-Path $root "tools\docker_autostart.ps1") $fixture
Set-Content -LiteralPath (Join-Path $fixture "docker-compose.yml") -Value "services: {}" -Encoding ascii
$fakeDocker = Join-Path $fixture "fake-docker.cmd"
$fakeDesktop = Join-Path $fixture "fake-desktop.cmd"

@'
@echo off
echo %*>>"%VISION_AUTOSTART_CALLS%"
if "%1"=="version" (
  echo linux
  exit /b 0
)
if "%1"=="compose" exit /b 0
exit /b 1
'@ | Set-Content -LiteralPath $fakeDocker -Encoding ascii
"@echo off`r`nexit /b 0" | Set-Content -LiteralPath $fakeDesktop -Encoding ascii
$calls = Join-Path $fixture "calls.txt"
$env:VISION_AUTOSTART_CALLS = $calls

try {
    & (Join-Path $fixture "docker_autostart.ps1") -ProjectRoot $fixture -DockerExecutable $fakeDocker -DockerDesktopExecutable $fakeDesktop
    if ($LASTEXITCODE -ne 0) { throw "Autostart helper returned $LASTEXITCODE" }
    $callText = Get-Content -LiteralPath $calls -Raw
    if ($callText -notmatch "compose config --quiet") { throw "Compose configuration was not validated" }
    if ($callText -notmatch "compose up -d --no-build") { throw "Autostart must use the saved images without a build" }
    $log = Get-Content -LiteralPath (Join-Path $fixture "data\logs\docker-autostart.log") -Raw
    if ($log -notmatch "Startup command completed") { throw "Successful startup was not logged" }

    Remove-Item -LiteralPath $calls -Force
    & (Join-Path $fixture "docker_autostart.ps1") -ProjectRoot $fixture -DockerExecutable $fakeDocker -CheckOnly
    if ($LASTEXITCODE -ne 0) { throw "Check-only mode returned $LASTEXITCODE" }
    if (Test-Path -LiteralPath $calls) { throw "Check-only mode must not call Docker" }

    $missingRoot = Join-Path $fixture "missing"
    & (Join-Path $fixture "docker_autostart.ps1") -ProjectRoot $missingRoot -DockerExecutable $fakeDocker -CheckOnly 2>$null
    if ($LASTEXITCODE -eq 0) { throw "A missing project directory must fail" }

    Write-Output "PASS: Docker autostart starts saved images, logs outcome, supports safe checks and rejects a missing project"
}
finally {
    Remove-Item Env:VISION_AUTOSTART_CALLS -ErrorAction SilentlyContinue
    $resolved = [IO.Path]::GetFullPath($fixture)
    if (-not $resolved.StartsWith([IO.Path]::GetFullPath([IO.Path]::GetTempPath())) -or (Split-Path $resolved -Leaf) -notlike "vision-autostart-test-*") {
        throw "Unsafe temporary path"
    }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}
