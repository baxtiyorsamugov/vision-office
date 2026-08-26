# Vision Office: installation on another computer

The project includes a single installer, fixed dependency versions, local face models, and automatic runtime selection. It runs on NVIDIA GPU when the card is compatible and switches to CPU when it is not.

## 1. Requirements on the target computer

- Windows 10/11, 64-bit
- Python 3.10, 64-bit, with `Add Python to PATH` enabled
- Microsoft Visual C++ 2015-2022 Redistributable
- Current NVIDIA driver only when GPU acceleration is needed

The CUDA Toolkit is not required. PyTorch supplies the needed CUDA runtime files.

## 2. Prepare the transfer kit on the source computer

Choose one profile for the target computer:

| Target hardware | Profile |
| --- | --- |
| No NVIDIA GPU | `cpu` |
| GTX 10 series, including GTX 1050 Ti | `pascal` |
| GTX 16 series, RTX 20/30/40/50 series | `modern` |

To make an offline kit, download the selected wheels before creating the archive:

```powershell
cd C:\projects\acs2
.\prepare_offline_wheels.ps1 -Profile pascal
.\create_transfer_archive.ps1
```

Replace `pascal` with `cpu` or `modern` when appropriate. Copy `acs2-transfer.zip` to the flash drive.

The archive contains project code, RTSP settings, attendance database, employee photos, `yolov8n-face.pt`, and local InsightFace models. Do not share it outside the trusted installation team.

## 3. Install on the target computer

Extract `acs2-transfer.zip`, for example to `C:\VisionOffice`. Open PowerShell in that folder:

```powershell
cd C:\VisionOffice
Set-ExecutionPolicy -Scope Process Bypass
.\install_vision_office.ps1
```

The installer creates `.venv`, installs fixed libraries, detects the NVIDIA compute capability, verifies the selected runtime, and then installs the application.

For an offline kit, run:

```powershell
.\install_vision_office.ps1 -Offline
```

Use `-Offline` only when `offline-wheels` was prepared for the same target profile.

## 4. Runtime selection

- NVIDIA `compute capability 7.5` and higher: modern CUDA 12.8 profile.
- NVIDIA `compute capability 6.x`: Pascal CUDA 11.8 profile.
- No compatible NVIDIA GPU, missing driver, or failed GPU verification: CPU profile.

The installer verifies GPU support after installation. With `-Profile auto` (the default), any GPU setup failure automatically falls back to CPU without leaving a partially configured environment.

## 5. Launch

```powershell
.\.venv\Scripts\Activate.ps1
python main.py
```

For the administration panel:

```powershell
streamlit run ui\app.py --server.port 8501
```

Open http://127.0.0.1:8501.

## Manual profile override

Use this only when you know the hardware profile:

```powershell
.\install_vision_office.ps1 -Profile cpu
.\install_vision_office.ps1 -Profile pascal
.\install_vision_office.ps1 -Profile modern
```
