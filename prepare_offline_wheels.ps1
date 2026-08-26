param(
    [ValidateSet("cpu", "modern", "pascal")]
    [string]$Profile = "cpu"
)

$ErrorActionPreference = "Stop"
$projectRoot = $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$wheelDirectory = Join-Path $projectRoot "offline-wheels"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Create the source computer .venv before preparing offline wheels."
}

function Download-Package {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)

    & $python -m pip download --only-binary=:all: --dest $wheelDirectory @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Package download failed: $($Arguments -join ' ')"
    }
}

New-Item -ItemType Directory -Force -Path $wheelDirectory | Out-Null
Download-Package -r (Join-Path $projectRoot "requirements.txt")

switch ($Profile) {
    "modern" {
        Download-Package --index-url https://download.pytorch.org/whl/cu128 torch==2.11.0+cu128 torchvision==0.26.0+cu128
        Download-Package onnxruntime-gpu==1.23.2
    }
    "pascal" {
        Download-Package --index-url https://download.pytorch.org/whl/cu118 torch==2.3.1+cu118 torchvision==0.18.1+cu118
        Download-Package onnxruntime-gpu==1.17.3
    }
    default {
        Download-Package --index-url https://download.pytorch.org/whl/cpu torch==2.11.0+cpu torchvision==0.26.0+cpu
        Download-Package onnxruntime==1.23.2
    }
}

Write-Host "Offline wheels are ready for profile: $Profile" -ForegroundColor Green
