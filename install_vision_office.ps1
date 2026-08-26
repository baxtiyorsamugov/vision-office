param(
    [ValidateSet("auto", "cpu", "modern", "pascal")]
    [string]$Profile = "auto"
)

$ErrorActionPreference = "Stop"
$env:PIP_NO_INDEX = $null
$env:HTTP_PROXY = $null
$env:HTTPS_PROXY = $null
$env:ALL_PROXY = $null
$env:http_proxy = $null
$env:https_proxy = $null
$env:all_proxy = $null
$projectRoot = $PSScriptRoot
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"

function Invoke-Python {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)

    & $venvPython @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed: python $($Arguments -join ' ')"
    }
}

function Install-Pip {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)

    Invoke-Python -m pip install --isolated --upgrade @Arguments
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

function Get-ProfileRequirementsFile {
    param([string]$SelectedProfile)

    $profileFiles = @{
        "cpu" = "requirements-cpu.txt"
        "modern" = "requirements-gpu.txt"
        "pascal" = "requirements-gpu-pascal.txt"
    }
    return Join-Path $projectRoot $profileFiles[$SelectedProfile]
}

function Install-BaseDependencies {
    param([string]$SelectedProfile)

    $profileFile = Get-ProfileRequirementsFile $SelectedProfile
    Install-Pip -c $profileFile -r (Join-Path $projectRoot "requirements.txt")
}

function Install-RuntimeProfile {
    param([string]$SelectedProfile)

    Invoke-Python -m pip uninstall -y torch torchvision onnxruntime onnxruntime-gpu
    $profileFile = Get-ProfileRequirementsFile $SelectedProfile

    switch ($SelectedProfile) {
        "modern" {
            Install-Pip -c $profileFile --index-url https://download.pytorch.org/whl/cu128 torch==2.11.0+cu128 torchvision==0.26.0+cu128
            Install-Pip -c $profileFile onnxruntime-gpu==1.23.2
        }
        "pascal" {
            Install-Pip -c $profileFile --index-url https://download.pytorch.org/whl/cu118 torch==2.3.1+cu118 torchvision==0.18.1+cu118
            Install-Pip -c $profileFile onnxruntime-gpu==1.17.1
        }
        default {
            Install-Pip -c $profileFile --index-url https://download.pytorch.org/whl/cpu torch==2.11.0+cpu torchvision==0.26.0+cpu
            Install-Pip -c $profileFile onnxruntime==1.23.2
        }
    }
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

Invoke-Python -m pip install --isolated --upgrade pip
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

Install-BaseDependencies $selectedProfile
Invoke-Python verify_install.py --profile $selectedProfile
Write-Host "Vision Office is ready. Selected profile: $selectedProfile" -ForegroundColor Green
