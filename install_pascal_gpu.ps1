$ErrorActionPreference = "Stop"

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Virtual environment was not found. Create .venv before installing the Pascal GPU profile."
}

function Invoke-Python {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)

    & $python @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed: python $($Arguments -join ' ')"
    }
}

Invoke-Python -m pip uninstall -y torch torchvision onnxruntime onnxruntime-gpu
$offlineWheels = Join-Path $PSScriptRoot "offline-wheels"
if (Test-Path -LiteralPath $offlineWheels) {
    Invoke-Python -m pip install --upgrade --force-reinstall --no-index --find-links $offlineWheels -r (Join-Path $PSScriptRoot "requirements-gpu-pascal.txt")
}
else {
    Invoke-Python -m pip install --upgrade --force-reinstall --index-url https://download.pytorch.org/whl/cu118 torch==2.3.1 torchvision==0.18.1
    Invoke-Python -m pip install --upgrade --force-reinstall onnxruntime-gpu==1.17.3
}

Invoke-Python test_gpu.py
