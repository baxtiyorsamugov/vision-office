"""Read-only, local reference-image diagnostics. No identification or enrollment."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.offline_video import readonly_session, write_report


def reference_manifest():
    from database.models import EduSchoolCatalogPerson as Person, EduSchoolReferencePhoto as Photo

    with readonly_session() as Session, Session() as session:
        people = session.query(Person.id, Person.person_type).filter(Person.active.is_(True)).all()
        rows = session.query(Photo.id, Photo.person_id, Photo.photo_path, Person.full_name, Person.person_type).join(
            Person, Photo.person_id == Person.id).filter(Photo.active.is_(True), Person.active.is_(True)).order_by(
            Photo.id).all()
        references = [dict(id=r.id, person_id=r.person_id, path=r.photo_path,
                           name=r.full_name, category=r.person_type) for r in rows]
        enrolled = {r.person_id for r in rows}
        return references, {"active": dict(Counter(p.person_type for p in people)),
                            "without_reference": dict(Counter(p.person_type for p in people if p.id not in enrolled))}


def safe_photo_path(root, stored):
    root = Path(root).resolve()
    path = (root / stored).resolve()
    if not path.is_relative_to(root / "data"):
        raise ValueError("Photo must be inside the project's data directory")
    return path


def image_metrics(image, boxes):
    """Heuristic review flags, not a calibrated biometric quality score."""
    import cv2
    import numpy as np

    height, width = image.shape[:2]
    result = {"width": width, "height": height, "face_count": len(boxes), "flags": []}
    if len(boxes) != 1:
        result["flags"].append("no_face" if not len(boxes) else "multiple_faces")
        return result
    raw = np.asarray(boxes[0][:4], dtype=float)
    if not np.isfinite(raw).all():
        result["flags"].append("invalid_box")
        return result
    x1, y1, x2, y2 = map(int, raw)
    x1, y1, x2, y2 = max(0, x1), max(0, y1), min(width, x2), min(height, y2)
    if x2 <= x1 or y2 <= y1:
        result["flags"].append("invalid_box")
        return result
    result["face_width"], result["face_height"] = x2 - x1, y2 - y1
    gray = cv2.cvtColor(image[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
    normalized = cv2.resize(gray, (112, 112), interpolation=cv2.INTER_AREA)
    sharpness = float(cv2.Laplacian(normalized, cv2.CV_64F).var())
    brightness = float(gray.mean())
    result.update(sharpness=round(sharpness, 1), brightness=round(brightness, 1))
    flags = result["flags"]
    if min(x2 - x1, y2 - y1) < 80:
        flags.append("small_face")
    if sharpness < 60:
        flags.append("low_detail")
    if brightness < 45:
        flags.append("dark")
    elif brightness > 210:
        flags.append("bright")
    if float(np.mean((gray <= 5) | (gray >= 250))) > .20:
        flags.append("clipping")
    if x1 <= 1 or y1 <= 1 or x2 >= width - 1 or y2 >= height - 1:
        flags.append("edge")
    return result


def audit_photo(reference, root, output, index, detector):
    import cv2
    import numpy as np
    from PIL import Image, ImageOps

    row = {k: reference[k] for k in ("id", "person_id", "name", "category")}
    try:
        source = safe_photo_path(root, reference["path"])
        if source.stat().st_size > 10 * 1024 * 1024:
            raise ValueError("Photo exceeds the 10 MiB audit limit")
        with Image.open(source) as original:
            if original.width * original.height > 25_000_000:
                raise ValueError("Photo exceeds the 25 megapixel audit limit")
            # Match OpenCV's orientation handling for the measured pixels.
            rgb = ImageOps.exif_transpose(original).convert("RGB")
        image = cv2.cvtColor(np.asarray(rgb), cv2.COLOR_RGB2BGR)
        boxes, _ = detector.detect(image, max_num=0)
        row.update(image_metrics(image, boxes))
        rgb.thumbnail((160, 160))
        name = f"media/{index:05d}.jpg"
        rgb.save(output / name, quality=85)
        row["thumbnail"] = name
    except (OSError, ValueError, Image.DecompressionBombError):
        row["flags"] = ["unreadable"]
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        raise SystemExit("Choose a new output directory; existing audits are preserved")
    model = Path.cwd() / "models/insightface/models/buffalo_l/det_10g.onnx"
    if not model.is_file():
        raise SystemExit("Existing local detection model is required")
    refs, coverage = reference_manifest()
    import cv2
    import onnxruntime as ort
    from insightface.model_zoo import get_model

    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    cv2.setNumThreads(1)
    # The approved SCRFD export has static output annotations for 640px; its
    # supported 320px input produces harmless shape warnings on each inference.
    options.log_severity_level = 3
    detector = get_model(str(model), providers=["CPUExecutionProvider"], sess_options=options)
    detector.prepare(ctx_id=-1, input_size=(320, 320), det_thresh=.5)
    (output / "media").mkdir(parents=True)
    rows = []
    for i, reference in enumerate(refs):
        rows.append(audit_photo(reference, Path.cwd(), output, i, detector))
        if (i + 1) % 100 == 0:
            print(json.dumps({"checked": i + 1, "total": len(refs)}), flush=True)
    report = {"created_at": datetime.now(timezone.utc).isoformat(), "coverage": coverage,
              "scope": "active_eduschool_references", "photos": rows,
              "summary": {"checked": len(rows), "review": sum(bool(r["flags"]) for r in rows),
                          "flags": dict(Counter(f for r in rows for f in r["flags"]))}}
    (output / "results.json").write_text(json.dumps(report, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    write_report(report, output / "index.html", ROOT / "tools/templates/reference_quality.html")
    print(json.dumps(report["summary"]), flush=True)


if __name__ == "__main__":
    main()
