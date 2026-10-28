"""Copy an approved reference bundle without the photos of the given people.

Used when a catalog card changed after review (for example a corrected name), so the
importer blocks it: the rest of the bundle can still be imported without a new transfer.
The dropped decisions are removed from the copied review file as well, so the new bundle
passes the same validation as the original.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.import_reviewed_photos import read_json, validate_bundle  # noqa: E402


def filter_bundle(source: Path, output: Path, exclude: set[str]) -> dict:
    source, output = source.resolve(), output.resolve()
    if output.exists():
        raise ValueError("Use a new output directory")
    manifest = validate_bundle(source)
    review = read_json(source / "original-human-review.json")
    unknown = exclude - {item["person_id"] for item in manifest["items"]}
    if unknown:
        raise ValueError(f"Not in this bundle: {sorted(unknown)}")
    items = [item for item in manifest["items"] if item["person_id"] not in exclude]
    review["decisions"] = {k: v for k, v in review["decisions"].items() if v.get("person_id") not in exclude}
    raw = json.dumps(review, ensure_ascii=False).encode("utf-8")
    (output / "photos").mkdir(parents=True)
    for item in items:
        shutil.copy2(source / item["image"], output / item["image"])
    (output / "original-human-review.json").write_bytes(raw)
    manifest = dict(manifest, items=items, source_annotation_sha256=hashlib.sha256(raw).hexdigest())
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    validate_bundle(output, expected_count=len(items))
    summary = {"bundle": str(output), "photos": len(items), "people": len({i["person_id"] for i in items}),
               "excluded_people": len(exclude)}
    print(json.dumps(summary, ensure_ascii=False))
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--exclude", action="append", required=True, help="person_id, may be repeated")
    args = parser.parse_args()
    filter_bundle(args.bundle, args.output, set(args.exclude))


if __name__ == "__main__":
    main()
