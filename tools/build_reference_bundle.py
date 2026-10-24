"""Turn an exported photo-review decision file into a bundle for import_reviewed_photos.py.

Only frames the operator confirmed AND selected as references are included. Identities
come solely from those decisions; nothing is assigned by face similarity here.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.import_reviewed_photos import read_json, validate_bundle  # noqa: E402


def build(review: Path, decisions_file: Path, output: Path) -> dict:
    review, output = review.resolve(), output.resolve()
    if output.exists():
        raise ValueError("Use a new output directory")
    report = read_json(review / "manifest.json")
    raw = decisions_file.read_bytes()
    decisions = read_json(decisions_file)
    if (decisions.get("purpose") != "manual_photo_review" or decisions.get("version") != 1
            or decisions.get("applied") is not False
            or decisions.get("snapshot_id") != report["snapshot_id"]
            or decisions.get("dump_sha256") != report["dump_sha256"]):
        raise ValueError("Decision file does not belong to this review page")
    events = {row["id"]: row for row in report["events"]}
    people = {row["id"]: row for row in report["people"]}
    items = []
    for event_id, decision in decisions["decisions"].items():
        if decision.get("status") != "confirmed" or decision.get("reference_approved") is not True:
            continue
        event, person = events.get(event_id), people.get(decision.get("person_id"))
        if event is None or person is None or decision.get("image_sha256") != event["sha256"]:
            raise ValueError(f"Decision {event_id} does not match the review page")
        items.append({
            "event_id": event_id, "person_id": person["id"], "full_name": person["full_name"],
            "person_type": person["person_type"], "image": f"photos/{event['sha256']}.jpg", "sha256": event["sha256"],
            "camera": event["camera"], "event_at": event["at"], "width": event["width"], "height": event["height"],
            "human_decision": decision,
        })
    if not items:
        raise ValueError("No confirmed reference photos in the decision file")
    (output / "photos").mkdir(parents=True)
    for item in items:
        shutil.copy2(review / "media" / f"{item['sha256']}.jpg", output / item["image"])
    (output / "original-human-review.json").write_bytes(raw)
    manifest = {
        "version": 1, "purpose": "human_selected_reference_candidates",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_snapshot_id": report["snapshot_id"], "source_dump_sha256": report["dump_sha256"],
        "source_annotation_sha256": hashlib.sha256(raw).hexdigest(),
        "applied": False, "embeddings_computed": False,
        "identity_assignment_source": "User-provided review decisions, copied without facial comparison",
        "items": items,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    validate_bundle(output, expected_count=len(items))
    summary = {"bundle": str(output), "photos": len(items), "people": len({i["person_id"] for i in items})}
    print(json.dumps(summary, ensure_ascii=False))
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review", type=Path, required=True, help="Review page folder with manifest.json and media/")
    parser.add_argument("--decisions", type=Path, required=True, help="JSON exported from the review page")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build(args.review, args.decisions, args.output)


if __name__ == "__main__":
    main()
