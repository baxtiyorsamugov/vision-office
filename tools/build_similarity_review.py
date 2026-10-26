"""Build an unknown-face review page with the most similar catalog person as a hint.

Input is a copied export folder (one *.dump, events/, persons/), never the app DB.
The hint is only a preselection: the operator must compare and confirm each frame.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import re
from pathlib import Path
import shutil
import sys

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from build_photo_review import digest, isolated_dump, photo_path, render, snapshot_id  # noqa: E402


def unit(value):
    vector = np.asarray(value, dtype=np.float32)
    norm = float(np.linalg.norm(vector)) if vector.shape == (512,) else 0.0
    return vector / norm if norm > 0 and np.isfinite(vector).all() else None


def pick_spread(rows: list[dict], count: int) -> list[dict]:
    """Keep the most similar frame, then the frames furthest in time from those already kept."""
    if count <= 0:
        return []
    moment = {row["id"]: datetime.fromisoformat(re.sub(r"\.\d+", "", row["at"]).replace("Z", "+00:00")).timestamp()
              for row in rows}
    remaining = sorted(rows, key=lambda row: (-row["suggested_similarity"], row["id"]))
    kept = [remaining.pop(0)]
    while remaining and len(kept) < count:
        best = max(remaining, key=lambda row: (min(abs(moment[row["id"]] - moment[k["id"]]) for k in kept),
                                               row["suggested_similarity"]))
        remaining.remove(best)
        kept.append(best)
    return kept


def build(input_root: Path, output: Path, since: str, min_similarity: float,
          max_per_person: int = 6, reference_limit: int = 15):
    input_root, output = input_root.resolve(), output.resolve()
    if output.exists() or output.is_relative_to(input_root):
        raise ValueError("Use a new output directory outside the imported data")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}Z)?", since):
        raise ValueError("--since must look like 2026-10-23 or 2026-10-24T06:40Z (UTC)")
    dumps = list(input_root.glob("*.dump"))
    if len(dumps) != 1:
        raise ValueError("Exactly one *.dump is required in the input folder")
    with isolated_dump(input_root, dumps[0]) as query:
        people = query("SELECT id, full_name, person_type, active FROM eduschool_catalog_people ORDER BY full_name, id")
        references = query("""SELECT id, person_id, photo_path, source, embedding FROM eduschool_reference_photos
                              WHERE active ORDER BY id""")
        events = query(f"""SELECT e.id, e.camera_id, e.event_type, e.photo_path, e.created_at, o.embedding
            FROM recognition_events e JOIN unknown_face_observations o ON o.event_id = e.id
            WHERE e.person_type = 'unknown' AND e.created_at >= '{since}'::timestamptz ORDER BY e.created_at""")
    output.mkdir(parents=True)
    (output / "media").mkdir()
    copied, skipped = {}, Counter()

    def materialize(stored):
        try:
            path = photo_path(input_root, stored)
        except ValueError:
            return None
        if not path.is_file():
            return None
        key = path.as_posix()
        if key not in copied:
            checksum = digest(path)
            with Image.open(path) as image:
                width, height = image.size
            target = output / "media" / f"{checksum}.jpg"
            if not target.exists():
                shutil.copy2(path, target)
            copied[key] = {"image": f"media/{checksum}.jpg", "source_path": "data/" + path.relative_to(input_root).as_posix(),
                           "sha256": checksum, "width": width, "height": height}
        return dict(copied[key])

    lookup = {person["id"]: {**person, "references": []} for person in people}
    vectors, owners = [], []
    local_counts = Counter(reference["person_id"] for reference in references if reference["source"] == "local")
    for reference in references:
        person = lookup.get(reference["person_id"])
        vector = unit(reference["embedding"]) if reference["embedding"] else None
        if person is None:
            continue
        photo = materialize(reference["photo_path"])
        if photo:
            person["references"].append({"id": reference["id"], **photo})
        else:
            skipped["references"] += 1
        if vector is not None and person["active"]:
            vectors.append(vector)
            owners.append(person["id"])
    matrix = np.vstack(vectors)
    owners = np.asarray(owners)
    rows = []
    for event in events:
        vector = unit(event["embedding"]) if event["embedding"] else None
        if vector is None:
            skipped["no_embedding"] += 1
            continue
        scores = matrix @ vector
        best = int(np.argmax(scores))
        if scores[best] < min_similarity:
            skipped["below_similarity"] += 1
            continue
        rows.append({"id": event["id"], "camera": event["camera_id"], "kind": event["event_type"], "at": event["created_at"],
                     "prediction_name": None, "prediction_type": "unknown", "prediction_person_id": None,
                     "suggested_person_id": str(owners[best]), "suggested_similarity": round(float(scores[best]), 3),
                     "photo_path": event["photo_path"]})
    # Only as many frames per person as can still become references, spread over different moments.
    by_person: dict[str, list[dict]] = {}
    for row in rows:
        by_person.setdefault(row["suggested_person_id"], []).append(row)
    selected = []
    for person_id, person_rows in by_person.items():
        room = min(max_per_person, reference_limit - local_counts[person_id])
        kept = pick_spread(person_rows, room)
        skipped["over_person_cap"] += len(person_rows) - len(kept)
        selected.extend(kept)
    rows = []
    for row in selected:
        photo = materialize(row.pop("photo_path"))
        if photo is None:
            skipped["events"] += 1
            continue
        rows.append({**row, **photo})
    report = {"version": 1, "created_at": datetime.now(timezone.utc).isoformat(), "review_scope": "unknown",
              "dump_sha256": digest(dumps[0]), "people": list(lookup.values()), "events": rows,
              "skipped": dict(skipped), "missing_by_date": {}, "since": since, "min_similarity": min_similarity}
    report["snapshot_id"] = snapshot_id(report)
    (output / "manifest.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    render(output)
    print(json.dumps({"page": str(output / "index.html"), "frames": len(rows), "skipped": dict(skipped)}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--since", required=True, help="Only unknown events at or after this UTC date or time, e.g. 2026-10-23 or 2026-10-24T06:40Z")
    parser.add_argument("--min-similarity", type=float, default=0.35)
    parser.add_argument("--max-per-person", type=int, default=6, help="Frames per suggested person to review")
    parser.add_argument("--reference-limit", type=int, default=15, help="Local reference limit on the device")
    args = parser.parse_args()
    build(args.input_root, args.output, args.since, args.min_similarity, args.max_per_person, args.reference_limit)


if __name__ == "__main__":
    main()
