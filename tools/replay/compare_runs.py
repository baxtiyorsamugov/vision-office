"""Compare replay runs per camera from the recognition trace (no database needed).

runs.jsonl (written by replay_recordings.ps1 -Action Compare) lists each run:
{"camera": "replay_kirish", "profile": "prod", "start": <epoch>, "end": <epoch>, "params": "..."}.
For every run: recognised people, attempts, share without embedding, coin-flip
matches blocked by the margin guard, and the median attempts per track.
"""

from __future__ import annotations

import argparse
import html
import json
from collections import defaultdict
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from voting import split_tracks  # noqa: E402


def load(path: Path) -> list[dict]:
    rows = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
    return rows


def analyse_run(trace: list[dict], run: dict) -> dict:
    attempts = [a for a in trace if run["start"] <= a["t"] <= run["end"]]
    known = sorted({a.get("best_name") for a in attempts if a.get("decision") == "known"})
    blocked = [(a.get("best_name"), a.get("best_score"), a.get("second_score")) for a in attempts if a.get("decision") == "low-margin"]
    tracks = [t for t in split_tracks(attempts) if any(r.get("best_score", 0) >= 0.30 for r in t)]
    lengths = sorted(len(t) for t in tracks)
    return {
        **run,
        "attempts": len(attempts),
        "no_embedding": round(sum(a.get("decision") == "no-embedding" for a in attempts) / len(attempts), 2) if attempts else None,
        "known": known,
        "blocked": blocked,
        "tracks": len(tracks),
        "median_attempts": lengths[len(lengths) // 2] if lengths else 0,
        "valid": len(attempts) > 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True, help="replay/runtime/data")
    parser.add_argument("--runs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    runs = load(args.runs)
    traces = {}
    results = []
    for run in runs:
        camera = run["camera"]
        if camera not in traces:
            traces[camera] = load(args.data / "recognition_trace" / f"{camera}.jsonl")
        results.append(analyse_run(traces[camera], run))

    by_camera: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for item in results:
        by_camera[item["camera"]][item["profile"]].append(item)
    profiles = sorted({r["profile"] for r in results}, key=lambda p: (p != "prod", p))

    lines, rows = [], []
    total = defaultdict(set)
    for camera in sorted(by_camera):
        union = set()
        for profile in profiles:
            for run in by_camera[camera][profile]:
                union |= set(run["known"])
        for profile in profiles:
            runs_here = by_camera[camera][profile]
            people = set().union(*[set(r["known"]) for r in runs_here]) if runs_here else set()
            total[profile] |= {(camera, p) for p in people}
            per_run = " / ".join(str(len(r["known"])) for r in runs_here)
            attempts = " / ".join(str(r["attempts"]) for r in runs_here)
            noemb = " / ".join(f'{100 * r["no_embedding"]:.0f}%' if r["no_embedding"] is not None else "-" for r in runs_here)
            med = " / ".join(str(r["median_attempts"]) for r in runs_here)
            blocked = "; ".join(f"{n} {s:.3f} vs {o:.3f}" for r in runs_here for n, s, o in r["blocked"])
            only = sorted(people - set().union(*[set(r["known"]) for p2 in profiles if p2 != profile for r in by_camera[camera][p2]]))
            invalid = any(not r["valid"] for r in runs_here)
            lines.append(f"{camera:32} {profile:10} known/run {per_run:8} attempts {attempts:10} no-emb {noemb:10} att/track {med:6}"
                         + (" INVALID(no attempts)" if invalid else ""))
            rows.append(f"<tr><td>{html.escape(camera)}</td><td>{html.escape(profile)}</td><td><b>{per_run}</b></td><td>{len(union)}</td>"
                        f"<td>{attempts}</td><td>{noemb}</td><td>{med}</td><td>{html.escape(', '.join(only))}</td>"
                        f"<td>{html.escape(blocked)}</td><td>{'⚠ нет попыток' if invalid else ''}</td></tr>")
    summary = " | ".join(f"{p}: {len(total[p])} человек узнано" for p in profiles)
    lines.append("TOTAL " + summary)
    params = "".join(f"<li>{html.escape(p)}: {html.escape(next((r['params'] for r in results if r['profile'] == p), ''))}</li>" for p in profiles)
    page = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>Replay compare</title>
<style>body{{font:14px system-ui,sans-serif;margin:16px}}table{{border-collapse:collapse}}td,th{{border:1px solid #ccc;padding:4px 8px}}</style></head><body>
<h1>Сравнение режимов (каждая запись отдельно)</h1><ul>{params}</ul><p><b>{html.escape(summary)}</b></p>
<table><tr><th>Камера</th><th>Режим</th><th>Узнано людей (по прогонам)</th><th>Узнаваемых всего (оба режима)</th><th>Попыток</th>
<th>Без эмбеддинга</th><th>Попыток на трек</th><th>Узнаны только в этом режиме</th><th>Отсечено защитой (score vs 2-й)</th><th></th></tr>{"".join(rows)}</table>
</body></html>"""
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(page, encoding="utf-8")
    print("\n".join(lines))
    print(f"Report: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
