"""Replay exported pseudonymous galleries on frozen vectors, without live services."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.offline_video import write_report
from tools.anonymous_face_lab import source_asset
from tools.compare_video_roi import digest


def validate(source, session):
    if not source["complete"] or session.get("version") != 1:
        raise ValueError("A completed source report and version-1 session are required")
    if (session["source_sha256"] != source["source_sha256"]
            or session["report_created_at"] != source["created_at"]
            or session["threshold"] != source["threshold"]):
        raise ValueError("Session belongs to another report or threshold")
    rows = {r["id"]: r for r in source["observations"]}
    if len(rows) != len(source["observations"]):
        raise ValueError("Duplicate observation IDs")
    for row in rows.values():
        if row["vector"] is not None:
            vector = np.asarray(row["vector"])
            if vector.shape != (512,) or not np.isfinite(vector).all() or abs(np.linalg.norm(vector)-1) > 1e-5:
                raise ValueError("Invalid normalized vector")
    for key, state in session["sessions"].items():
        vi = int(key)
        if str(vi) != key or not 0 <= vi < len(source["videos"]):
            raise ValueError("Invalid source video")
        ids, refs = set(), set()
        for person in state["profiles"]:
            if not re.fullmatch(r"test-\d+", person["id"]) or person["id"] in ids or not person["refs"]:
                raise ValueError("Invalid or duplicate profile")
            ids.add(person["id"])
            for ref in person["refs"]:
                row = rows.get(ref)
                if not row or row["video"] != vi or not row["reference_ok"] or row["vector"] is None or ref in refs:
                    raise ValueError("Invalid, leaked or duplicate reference")
                refs.add(ref)
    return rows


def decide(scores, people, best_refs, threshold):
    accepted = np.flatnonzero(scores > threshold)
    if len(accepted) > 1:
        return {"status": "ambiguous", "candidates": [people[i] for i in accepted]}
    if len(accepted) == 1:
        i = int(accepted[0])
        return {"status": "matched", "person": people[i], "reference": best_refs[i]}
    return {"status": "unknown"}


def evaluate_gallery(observations, profiles, source_video, rows, threshold, gap):
    if not profiles or gap < 0 or not np.isfinite(gap):
        raise ValueError("A nonempty gallery and a finite nonnegative gap are required")
    refs = [ref for p in profiles for ref in p["refs"]]
    vectors = np.array([rows[ref]["vector"] for ref in refs], dtype=np.float64)
    times = [rows[ref]["time"] for ref in refs]
    groups, offset = [], 0
    for p in profiles:
        groups.append(list(range(offset, offset+len(p["refs"]))))
        offset += len(p["refs"])
    people = [p["id"] for p in profiles]

    def match(vector):
        similarities = vectors @ np.asarray(vector, dtype=np.float64)
        best = [max(group, key=lambda i: similarities[i]) for group in groups]
        return decide(np.array([similarities[i] for i in best]), people,
                      [refs[i] for i in best], threshold)

    control = Counter()
    for p in profiles:
        for ref in p["refs"]:
            result = match(rows[ref]["vector"])
            control["exact_reference_ok" if result.get("person") == p["id"] else result["status"]] += 1
    outcomes = []
    for row in observations:
        if row["video"] == source_video and any(abs(row["time"]-t) <= gap+1e-8 for t in times):
            result = {"status": "excluded"}
        elif row["vector"] is None:
            result = {"status": "no_embedding"}
        else:
            result = match(row["vector"])
        outcomes.append(result)
    return outcomes, dict(control)


def build(args):
    source = json.loads(args.source_report.read_text(encoding="utf-8"))
    session = json.loads(args.session.read_text(encoding="utf-8"))
    rows = validate(source, session)
    if args.output.exists():
        raise ValueError("Use a new output directory")
    report = {"created_at": datetime.now(timezone.utc).isoformat(), "threshold": source["threshold"],
              "gap_seconds": args.gap_seconds, "sample_fps": source["sample_fps"],
              "session_sha256": digest(args.session), "source_sha256": digest(args.source_report),
              "videos": source["videos"], "galleries": [],
              "observations": [{k: r[k] for k in ("id", "video", "frame", "time", "box", "image", "context")}
                               for r in source["observations"]]}
    for row in report["observations"]:
        row["outcomes"] = {}
    for key, state in session["sessions"].items():
        profiles = state["profiles"]
        if not profiles:
            continue
        vi = int(key)
        outcomes, control = evaluate_gallery(source["observations"], profiles, vi, rows,
                                              source["threshold"], args.gap_seconds)
        counts = [Counter() for _ in source["videos"]]
        identities = {p["id"]: {"id": p["id"], "refs": p["refs"], "same_clip": 0,
                                "other_clips": 0, "ambiguous": 0} for p in profiles}
        for row, result in zip(report["observations"], outcomes):
            row["outcomes"][key] = result
            counts[row["video"]][result["status"]] += 1
            if result["status"] == "matched":
                identities[result["person"]]["same_clip" if row["video"] == vi else "other_clips"] += 1
            for person in result.get("candidates", []):
                identities[person]["ambiguous"] += 1
        report["galleries"].append({"key": key, "source_video": vi, "profiles": list(identities.values()),
                                    "counts": list(map(dict, counts)), "control": control})
    if not report["galleries"]:
        raise ValueError("No enrolled profiles in this session")
    (args.output / "media").mkdir(parents=True)
    assets = {v["video"] for v in report["videos"]}
    assets.update(r[k] for r in report["observations"] for k in ("image", "context"))
    for asset in assets:
        shutil.copy2(source_asset(args.source_report.parent, asset), args.output / asset)
    (args.output / "results.json").write_text(json.dumps(report, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    write_report(report, args.output / "index.html", ROOT / "tools/templates/test_identity_review.html")
    print(json.dumps([{"source": source["videos"][g["source_video"]]["name"], "profiles": len(g["profiles"]),
                       "counts": g["counts"], "control": g["control"]} for g in report["galleries"]]), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-report", type=Path, required=True)
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--gap-seconds", type=float, default=2)
    build(parser.parse_args())


if __name__ == "__main__":
    main()
