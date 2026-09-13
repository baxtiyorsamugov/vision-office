"""Small Windows helper that starts the PowerShell Docker autostart routine."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


VERSION = "1.0.0"


def bundled_script_path() -> Path:
    """Resolve the PowerShell payload in source and PyInstaller modes."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "tools" / "docker_autostart.ps1"  # type: ignore[attr-defined]
    return Path(__file__).resolve().parents[1] / "tools" / "docker_autostart.ps1"


def default_project_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent.parent
    return Path(__file__).resolve().parents[1]


def powershell_path() -> str:
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    candidate = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(candidate) if candidate.is_file() else "powershell.exe"


def main() -> int:
    parser = argparse.ArgumentParser(description="Start the saved Vision Office Docker stack.")
    parser.add_argument("--project-root", type=Path, default=default_project_root())
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--check", action="store_true", help="Validate files only; do not start Docker.")
    parser.add_argument("--version", action="version", version=f"Vision Office Autostart {VERSION}")
    args = parser.parse_args()

    script = bundled_script_path()
    if not script.is_file():
        return 2
    if args.timeout_seconds < 10 or args.timeout_seconds > 900:
        parser.error("--timeout-seconds must be between 10 and 900")

    command = [
        powershell_path(),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-ProjectRoot",
        str(args.project_root.resolve()),
        "-TimeoutSeconds",
        str(args.timeout_seconds),
    ]
    if args.check:
        command.append("-CheckOnly")

    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    completed = subprocess.run(
        command,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        startupinfo=startupinfo,
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
