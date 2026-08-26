param(
    [ValidateSet("auto", "cpu", "modern", "pascal")]
    [string]$Profile = "auto",
    [switch]$Offline
)

$ErrorActionPreference = "Stop"
$projectRoot = $PSScriptRoot
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$offlineWheels = Join-Path $projectRoot "offline-wheels"

function Invoke-Python {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)

    & $venvPython @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed: python $($Arguments -join ' ')"
    }
}

function Install-Pip {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)

    Invoke-Python -m pip install --upgrade --force-reinstall @Arguments
}

function Get-NvidiaComputeCapability {
    $nvidiaSmi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    if ($null -eq $nvidiaSmi) {
        return $null
    }

    $result = & $nvidiaSmi.Source "--query-gpu=compute_cap" "--format=csv,noheader" 2>$null | Select-Object -First 1
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($result)) {
        return $null
    }

    $match = [regex]::Match($result, "\d+\.\d+")
    if (-not $match.Success) {
        return $null
    }

    return [double]::Parse($match.Value, [Globalization.CultureInfo]::InvariantCulture)
}

function Select-RuntimeProfile {
    if ($Profile -ne "auto") {
        return $Profile
    }

    $capability = Get-NvidiaComputeCapability
    if ($null -eq $capability) {
        return "cpu"
    }
    if ($capability -ge 7.5) {
        return "modern"
    }
    if ($capability -ge 6.0) {
        return "pascal"
    }
    return "cpu"
}

function Install-BaseDependencies {
    if ($Offline) {
        Install-Pip --no-index --find-links $offlineWheels -r (Join-Path $projectRoot "requirements.txt")
    }
    else {
        Install-Pip -r (Join-Path $projectRoot "requirements.txt")
    }
}

function Install-RuntimeProfile {
    param([string]$SelectedProfile)

    Invoke-Python -m pip uninstall -y torch torchvision onnxruntime onnxruntime-gpu

    if ($Offline) {
        $profileFiles = @{
            "cpu" = "requirements-cpu.txt"
            "modern" = "requirements-gpu.txt"
            "pascal" = "requirements-gpu-pascal.txt"
        }
        $profileFile = Join-Path $projectRoot $profileFiles[$SelectedProfile]
        Install-Pip --no-index --find-links $offlineWheels -r $profileFile
        return
    }

    switch ($SelectedProfile) {
        "modern" {
            Install-Pip --index-url https://download.pytorch.org/whl/cu128 torch==2.11.0+cu128 torchvision==0.26.0+cu128
            Install-Pip onnxruntime-gpu==1.23.2
        }
        "pascal" {
            Install-Pip --index-url https://download.pytorch.org/whl/cu118 torch==2.3.1+cu118 torchvision==0.18.1+cu118
            Install-Pip onnxruntime-gpu==1.17.3
        }
        default {
            Install-Pip --index-url https://download.pytorch.org/whl/cpu torch==2.11.0+cpu torchvision==0.26.0+cpu
            Install-Pip onnxruntime==1.23.2
        }
    }
}

if ($Offline -and -not (Test-Path -LiteralPath $offlineWheels)) {
    throw "Offline mode needs the offline-wheels folder in the project directory."
}

if (-not (Test-Path -LiteralPath $venvPython)) {
    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($null -ne $pyLauncher) {
        & $pyLauncher.Source -3.10 -m venv (Join-Path $projectRoot ".venv")
    }
    else {
        python -m venv (Join-Path $projectRoot ".venv")
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Python 3.10 is required. Install Python 3.10 x64 and run this script again."
    }
}

$pythonVersion = (& $venvPython -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')").Trim()
if ($pythonVersion -ne "3.10") {
    throw "Vision Office requires Python 3.10 x64. Current environment: $pythonVersion"
}

Invoke-Python -m pip install --upgrade pip
$selectedProfile = Select-RuntimeProfile
Write-Host "Selected runtime profile: $selectedProfile" -ForegroundColor Cyan

try {
    Install-RuntimeProfile $selectedProfile
    if ($selectedProfile -ne "cpu") {
        Invoke-Python test_gpu.py
    }
}
catch {
    if ($Profile -ne "auto" -or $selectedProfile -eq "cpu") {
        throw
    }

    Write-Host "GPU setup failed. Switching to CPU runtime." -ForegroundColor Yellow
    Install-RuntimeProfile "cpu"
    $selectedProfile = "cpu"
}

Install-BaseDependencies
Invoke-Python verify_install.py --profile $selectedProfile
Write-Host "Vision Office is ready. Selected profile: $selectedProfile" -ForegroundColor Green
