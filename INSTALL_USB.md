# Установка через флешку

## 1. Подготовить флешку на этом ПК

Скопируйте на флешку папку проекта `acs2`, включая `data`, `config`, `yolov8n-face.pt` и `requirements*.txt`.
Не копируйте `.venv`.

В папке проекта выполните:

```powershell
.\.venv\Scripts\Activate.ps1
New-Item -ItemType Directory -Force offline-wheels
python -m pip download --only-binary=:all: --dest offline-wheels torch==2.11.0+cu128 torchvision==0.26.0+cu128 --index-url https://download.pytorch.org/whl/cu128
python -m pip download --only-binary=:all: --dest offline-wheels -r requirements.txt
```

Скопируйте `offline-wheels` на флешку внутрь папки `acs2`.

## 2. Подготовить новый ПК

- Windows 10/11 64-bit
- Python 3.10 64-bit с включённым `Add Python to PATH`
- актуальный драйвер NVIDIA для видеокарты
- Microsoft Visual C++ 2015-2022 Redistributable

Скопируйте папку `acs2` с флешки, например в `C:\VisionOffice`.

## 3. Установить зависимости без интернета

Откройте PowerShell:

```powershell
cd C:\VisionOffice
Set-ExecutionPolicy -Scope Process Bypass
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --no-index --find-links .\offline-wheels torch==2.11.0+cu128 torchvision==0.26.0+cu128
python -m pip install --no-index --find-links .\offline-wheels -r requirements.txt
python -m pip uninstall -y onnxruntime onnxruntime-gpu
python -m pip install --no-index --find-links .\offline-wheels --no-deps onnxruntime-gpu==1.23.2
```

## 4. Проверить и запустить

```powershell
python test_gpu.py
python -c "import onnxruntime as ort; print(ort.get_available_providers())"
python main.py
```

Должны появиться `True` для CUDA и `CUDAExecutionProvider` в списке ONNX Runtime.
Для панели управления: `streamlit run ui\app.py` и открыть `http://localhost:8501`.

## Если в консоли красные сообщения CUDA/cuDNN

Сообщения о `cudnn64_9.dll` или `cublasLt64_12.dll` означают, что ONNX Runtime не получил CUDA-библиотеки. Камера может работать, но распознавание перейдёт на CPU и будет медленнее.

На ПК с NVIDIA видеокартой закройте программу, активируйте `.venv` и переустановите GPU-пакеты:

```powershell
cd C:\VisionOffice
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade --force-reinstall -r requirements-gpu.txt
python test_gpu.py
```

Также обновите драйвер NVIDIA. Устанавливать отдельный CUDA Toolkit не нужно: необходимые библиотеки поставляются вместе с PyTorch. Если видеокарты NVIDIA нет, ничего устанавливать не надо: Vision Office автоматически использует CPU без красных сообщений.
# Creating the archive on the source computer

Use the prepared archive script instead of adding files to WinRAR manually. It creates a safe snapshot of `data/office.db`, so the program may stay open.

```powershell
cd C:\projects\acs2
.\create_transfer_archive.ps1
```

Copy the resulting `C:\projects\acs2\acs2-transfer.zip` to the flash drive.
