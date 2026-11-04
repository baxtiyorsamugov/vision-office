"""Build the replay bench settings.yaml from production settings and a recordings folder.

Every recording ``<name>.<ext>`` becomes camera ``replay_<name>`` reading
``rtsp://replay-rtsp:8554/<name>``. All recognition settings (thresholds, AI,
gallery sources) are copied unchanged from production. Every outbound sender is
disabled, so the bench can never deliver attendance even if it is misconfigured.
"""

from __future__ import annotations

import argparse
import copy
import re
import sys
from pathlib import Path

import yaml


VIDEO_EXTENSIONS = {".mp4", ".avi", ".mkv", ".mov", ".ts", ".dav", ".h264", ".h265", ".264", ".265"}
NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,60}$")
RTSP_HOST = "replay-rtsp:8554"
EXIT_HINTS = ("chiqish", "exit", "out")


def find_recordings(directory: Path) -> list[Path]:
    files = sorted(p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS)
    bad = [p.name for p in files if not NAME_PATTERN.match(p.stem)]
    if bad:
        print("SKIPPED (rename to Latin letters, digits, '-' or '_', no spaces or '$'): " + ", ".join(bad),
              file=sys.stderr)
    files = [p for p in files if NAME_PATTERN.match(p.stem)]
    stems = [p.stem.lower() for p in files]
    duplicates = sorted({s for s in stems if stems.count(s) > 1})
    if duplicates:
        raise SystemExit("Two recordings share a name with different extensions: " + ", ".join(duplicates))
    return files


def select_recordings(recordings: list[Path], only: list[str]) -> list[Path]:
    wanted = {name.strip().lower() for item in only for name in item.split(",") if name.strip()}
    if not wanted:
        return recordings
    known = {p.stem.lower(): p for p in recordings}
    missing = sorted(wanted - known.keys())
    if missing:
        raise SystemExit("Recording not found: " + ", ".join(missing) + ". Available: " + ", ".join(sorted(known)))
    return [p for p in recordings if p.stem.lower() in wanted]


# Final production thresholds (decided 2026-10-29 / 2026-11-03). A developer PC
# may have an older settings.yaml, so the bench pins them unless overridden.
PRODUCTION_EDGE_THRESHOLD = 0.40
PRODUCTION_EDUSCHOOL_THRESHOLD = 0.50


def build_settings(production: dict, recordings: list[Path], *, edge_threshold: float | None = None,
                   eduschool_threshold: float | None = None) -> dict:
    settings = copy.deepcopy(production or {})
    if edge_threshold is not None:
        settings.setdefault("edge_integration", {})["recognition_threshold"] = float(edge_threshold)
    if eduschool_threshold is not None:
        settings.setdefault("eduschool_catalog", {})["recognition_threshold"] = float(eduschool_threshold)
    cameras = []
    for path in recordings:
        name = path.stem
        lowered = name.lower()
        cameras.append({
            "id": f"replay_{lowered}"[:100],
            "name": f"REPLAY {name}",
            "rtsp_url": f"rtsp://{RTSP_HOST}/{name}",
            "is_active": True,
            "event_type": "exit" if any(hint in lowered for hint in EXIT_HINTS) else "entry",
            "location": "Replay bench",
        })
    settings["cameras"] = cameras

    # Recognition stays identical; only delivery is switched off.
    turnstile = settings.get("eduschool_turnstile")
    if isinstance(turnstile, dict):
        turnstile["enabled"] = False
        turnstile["students_enabled"] = False
        turnstile["device_ids"] = {}
    for section in ("notifications", "telegram", "monitoring", "health"):
        if isinstance(settings.get(section), dict):
            settings[section]["enabled"] = False
    if isinstance(settings.get("health"), dict):
        settings["health"]["telegram_enabled"] = False
    # Keep unknown-face embeddings so the replay report can name the closest people.
    unknown = settings.get("unknown_visitors")
    if not isinstance(unknown, dict):
        unknown = settings["unknown_visitors"] = {}
    unknown["enabled"] = True
    catalog = settings.get("eduschool_catalog")
    if isinstance(catalog, dict):
        # The catalog sync service is not started; this only documents intent.
        catalog["sync_interval_seconds"] = max(int(catalog.get("sync_interval_seconds", 3600)), 86400)
    return settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--production", type=Path, required=True, help="Production config/settings.yaml")
    parser.add_argument("--recordings", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--edge-threshold", type=float, default=PRODUCTION_EDGE_THRESHOLD)
    parser.add_argument("--eduschool-threshold", type=float, default=PRODUCTION_EDUSCHOOL_THRESHOLD)
    parser.add_argument("--only", action="append", default=[],
                        help="Use only these recordings (file name without extension); repeatable or comma-separated")
    args = parser.parse_args(argv)

    with args.production.open("r", encoding="utf-8") as source:
        production = yaml.safe_load(source) or {}
    recordings = find_recordings(args.recordings)
    recordings = select_recordings(recordings, args.only)
    if not recordings:
        raise SystemExit(f"No recordings found in {args.recordings} ({', '.join(sorted(VIDEO_EXTENSIONS))})")
    settings = build_settings(production, recordings, edge_threshold=args.edge_threshold,
                              eduschool_threshold=args.eduschool_threshold)
    print(f"Thresholds: eduschool_catalog={settings['eduschool_catalog']['recognition_threshold']}, "
          f"edge_integration={settings['edge_integration']['recognition_threshold']}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="\n") as target:
        target.write("# Generated by tools/replay/make_config.py for the replay bench. Do not copy to production.\n")
        yaml.safe_dump(settings, target, allow_unicode=True, sort_keys=False)
    for camera in settings["cameras"]:
        print(f"{camera['id']}: {camera['rtsp_url']} ({camera['event_type']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
