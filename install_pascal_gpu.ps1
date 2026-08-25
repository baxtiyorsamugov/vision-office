$ErrorActionPreference = "Stop"

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Virtual environment was not found. Create .venv before installing the Pascal GPU profile."
}

& $python -m pip uninstall -y torch torchvision onnxruntime onnxruntime-gpu
$installOptions = @("--upgrade", "--force-reinstall")
$offlineWheels = Join-Path $PSScriptRoot "offline-wheels"
if (Test-Path -LiteralPath $offlineWheels) {
    $installOptions += @("--no-index", "--find-links", $offlineWheels)
}

& $python -m pip install @installOptions -r (Join-Path $PSScriptRoot "requirements-gpu-pascal.txt")
& $python test_gpu.py
