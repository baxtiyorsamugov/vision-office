"""Resource sampling by the supervisor, outside the video/inference loops."""
import csv
import io
import shutil
import subprocess

import psutil


def sample_resources():
    memory = psutil.virtual_memory()
    disk = shutil.disk_usage("data")
    result = {"cpu_percent": psutil.cpu_percent(), "memory_percent": memory.percent,
              "memory_available_gb": round(memory.available / 1024**3, 1),
              "disk_free_gb": round(disk.free / 1024**3, 1), "gpus": []}
    try:
        command = subprocess.run([
            "nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total",
            "--format=csv,noheader,nounits",
        ], capture_output=True, text=True, timeout=1, check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for row in csv.reader(io.StringIO(command.stdout)):
            if len(row) == 4:
                result["gpus"].append({"name": row[0].strip(), "utilization": row[1].strip(),
                                       "memory_used_mb": row[2].strip(), "memory_total_mb": row[3].strip()})
    except (OSError, subprocess.SubprocessError):
        pass
    return result
