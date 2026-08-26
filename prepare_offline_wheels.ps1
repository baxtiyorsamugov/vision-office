param(
    [ValidateSet("cpu", "modern", "pascal")]
    [string]$Profile = "cpu"
)

$ErrorActionPreference = "Stop"
$env:PIP_NO_INDEX = $null
$projectRoot = $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$wheelDirectory = Join-Path $projectRoot "offline-wheels"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Create the source computer .venv before preparing offline wheels."
}

function Download-Package {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)

    & $python -m pip download --isolated --index-url https://pypi.org/simple --only-binary=:all: --find-links $wheelDirectory --dest $wheelDirectory @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Package download failed: $($Arguments -join ' ')"
    }
}

function Build-InsightFaceWheel {
    & $python -m pip wheel --isolated --index-url https://pypi.org/simple --no-deps --wheel-dir $wheelDirectory insightface==0.7.3
    if ($LASTEXITCODE -ne 0) {
        throw "InsightFace wheel build failed."
    }
}

New-Item -ItemType Directory -Force -Path $wheelDirectory | Out-Null

switch ($Profile) {
    "modern" {
        Download-Package --index-url https://download.pytorch.org/whl/cu128 torch==2.11.0+cu128 torchvision==0.26.0+cu128
        Download-Package onnxruntime-gpu==1.23.2
    }
    "pascal" {
        Download-Package --index-url https://download.pytorch.org/whl/cu118 torch==2.3.1+cu118 torchvision==0.18.1+cu118
        Download-Package onnxruntime-gpu==1.17.1
    }
    default {
        Download-Package --index-url https://download.pytorch.org/whl/cpu torch==2.11.0+cpu torchvision==0.26.0+cpu
        Download-Package onnxruntime==1.23.2
    }
}

Build-InsightFaceWheel
$temporaryRequirements = Join-Path ([System.IO.Path]::GetTempPath()) "vision-office-requirements.txt"
$baseRequirements = Get-Content (Join-Path $projectRoot "requirements.txt") | Where-Object { $_ -notmatch "^insightface==" }
$runtimeRequirements = switch ($Profile) {
    "modern" { @("torch==2.11.0+cu128", "torchvision==0.26.0+cu128") }
    "pascal" { @("torch==2.3.1+cu118", "torchvision==0.18.1+cu118") }
    default { @("torch==2.11.0+cpu", "torchvision==0.26.0+cpu") }
}
@($baseRequirements + $runtimeRequirements) | Set-Content $temporaryRequirements
try {
    Download-Package -r $temporaryRequirements
    Download-Package insightface==0.7.3
}
finally {
    Remove-Item -LiteralPath $temporaryRequirements -ErrorAction SilentlyContinue
}

Write-Host "Offline wheels are ready for profile: $Profile" -ForegroundColor Green
