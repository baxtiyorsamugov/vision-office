# Vision Office: classic PowerShell installation

This method is intended for a normal internet-connected Windows computer. The project is copied by flash drive; PowerShell then creates the environment and installs exact library versions directly from official indexes.

## 1. Prepare the target computer

- Windows 10/11, 64-bit
- Install Python 3.10 x64 from python.org and select `Add Python to PATH`
- Install Microsoft Visual C++ 2015-2022 Redistributable x64
- Install or update the NVIDIA driver only when GPU acceleration is required

The CUDA Toolkit is not required.

## 2. Copy the project

On the source computer, create the transfer archive:

```powershell
cd C:\projects\acs2
.\create_transfer_archive.ps1
```

Copy `acs2-transfer.zip` to the flash drive. On the target computer, extract it to `C:\VisionOffice`.

## 3. Install through PowerShell

Open PowerShell in the extracted folder and run:

```powershell
cd C:\VisionOffice
Set-ExecutionPolicy -Scope Process Bypass
.\install_vision_office.ps1
```

The script creates `.venv`, installs fixed package versions, detects the NVIDIA card, and verifies the installation before it completes.

## 4. Automatic runtime selection

- GTX 10 series, including GTX 1050 Ti: Pascal CUDA 11.8 profile.
- GTX 16 series and RTX cards: modern CUDA 12.8 profile.
- No compatible GPU, missing driver, or failed GPU verification: CPU profile automatically.

If the automatic check needs to be overridden:

```powershell
.\install_vision_office.ps1 -Profile cpu
.\install_vision_office.ps1 -Profile pascal
.\install_vision_office.ps1 -Profile modern
```

## 5. Launch

```powershell
cd C:\VisionOffice
.\.venv\Scripts\Activate.ps1
python main.py
```

For the administration panel:

```powershell
streamlit run ui\app.py --server.port 8501
```

Open http://127.0.0.1:8501.
