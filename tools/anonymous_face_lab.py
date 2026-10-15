"""Build a local pseudonymous reference experiment, without catalog or API access."""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.offline_video import crop_bounds, write_report
from tools.compare_video_roi import digest, metadata
from tools.faceid_lab import choose_face, local_child


def source_asset(directory, relative):
    parts = Path(relative).parts
    if len(parts) != 2 or parts[0] != "media":
        raise ValueError("Expected a relative media filename")
    path = (directory / relative).resolve()
    if path.parent != (directory / "media").resolve() or not path.is_file():
        raise ValueError("Expected an existing direct media asset")
    return path


def extract(frame, box, app):
    import cv2
    import numpy as np
    height, width = frame.shape[:2]
    a, b, c, d = box
    if not (0 <= a < c <= width and 0 <= b < d <= height) or min(c-a, d-b) < 48:
        raise ValueError("Invalid or undersized frozen face box")
    x1, y1, x2, y2 = crop_bounds(box, width, height)
    crop = frame[y1:y2, x1:x2]
    faces = app.get(crop)
    vector, reason = choose_face(faces, (crop.shape[1]/2, crop.shape[0]/2))
    sharpness = float(cv2.Laplacian(cv2.resize(cv2.cvtColor(frame[b:d, a:c], cv2.COLOR_BGR2GRAY),
                                              (112, 112)), cv2.CV_64F).var())
    # References are stricter than probes: a single face, >=64px and no frame-edge clipping.
    reference_ok = (vector is not None and len(faces) == 1 and min(c-a, d-b) >= 64
                    and a > 1 and b > 1 and c < width-1 and d < height-1)
    return {"vector": None if vector is None else (vector/np.linalg.norm(vector)).tolist(),
            "reason": reason, "reference_ok": reference_ok, "sharpness": round(sharpness, 2)}


def initial_reference(rows):
    candidates = [r for r in rows if r["reference_ok"]]
    if not candidates:
        return None
    return max(candidates, key=lambda r: (min(r["box"][2]-r["box"][0], r["box"][3]-r["box"][1]),
                                         r["sharpness"]))["id"]


def save(report, output):
    temporary = output / "results.tmp"
    temporary.write_text(json.dumps(report, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    temporary.replace(output / "results.json")
    write_report(report, output / "index.html", ROOT / "tools/templates/anonymous_face_lab.html")


def build(args):
    import cv2
    from core.ai.recognizer import FaceRecognizer, INSIGHTFACE_MODEL_ROOT

    if args.output.exists():
        raise ValueError("Use a new output folder")
    source = json.loads(args.source_report.read_text(encoding="utf-8"))
    if not source["complete"]:
        raise ValueError("The source experiment must be complete")
    source_dir = args.source_report.parent
    paths = []
    for video in source["videos"]:
        path = local_child(args.input, video["name"])
        meta = metadata(path)
        if digest(path) != video["sha256"] or meta["frames"] != video["frames"]:
            raise ValueError("Original recording differs from the source experiment")
        paths.append(path)
    os.environ["VISION_OFFICE_DEVICE"] = args.device
    cv2.setNumThreads(1)
    recognizer = FaceRecognizer(det_size=(320, 320), use_cuda=args.device == "cuda")
    if args.device == "cuda" and not recognizer.using_cuda:
        raise RuntimeError("Requested CUDA is unavailable")
    recognizer.sanity_check()
    pack = INSIGHTFACE_MODEL_ROOT / "models/buffalo_l"
    model_hashes = {p.name: digest(p) for p in (pack / "det_10g.onnx", pack / "w600k_r50.onnx")}
    if model_hashes != source["provenance"]["models"]:
        raise ValueError("Model weights differ from the control experiment")
    (args.output / "media").mkdir(parents=True)
    report = {"created_at": datetime.now(timezone.utc).isoformat(), "complete": False,
              "threshold": source["eduschool_threshold"], "device": args.device,
              "source_sha256": digest(args.source_report), "models": model_hashes,
              "sample_fps": source["sample_fps"], "skipped_small": source["skipped_small"],
              "videos": [], "observations": [], "initial_reference": None}
    copied = set()
    for vi, (video, path) in enumerate(zip(source["videos"], paths)):
        clean_video = {k: video[k] for k in ("name", "width", "height", "fps", "frames", "sha256", "video")}
        if not clean_video["video"]:
            raise ValueError("Attach browser media to the source lab first")
        shutil.copy2(source_asset(source_dir, clean_video["video"]), args.output / clean_video["video"])
        report["videos"].append(clean_video)
        by_frame = defaultdict(list)
        for row in source["observations"]:
            if row["video"] == vi:
                by_frame[row["frame"]].append(row)
        cap = cv2.VideoCapture(str(path))
        index, seen = 0, 0
        try:
            while cap.grab():
                if index in by_frame:
                    ok, frame = cap.retrieve()
                    if not ok:
                        raise RuntimeError("Frame decode failed")
                    for old in by_frame[index]:
                        # Allowlist drops all real names, person IDs and original gallery matches.
                        row = {k: old[k] for k in ("id", "video", "frame", "time", "box", "image", "context")}
                        row.update(extract(frame, row["box"], recognizer.app))
                        for key in ("image", "context"):
                            asset = row[key]
                            if asset not in copied:
                                shutil.copy2(source_asset(source_dir, asset), args.output / asset)
                                copied.add(asset)
                        report["observations"].append(row)
                        seen += 1
                index += 1
                if index % 500 == 0:
                    print(json.dumps({"video": vi+1, "frames": index, "faces": len(report["observations"])}), flush=True)
        finally:
            cap.release()
        if index != video["frames"] or seen != sum(map(len, by_frame.values())):
            raise RuntimeError("Incomplete decode")
        save(report, args.output)
    report["initial_reference"] = initial_reference(report["observations"])
    report["complete"] = True
    save(report, args.output)
    print(json.dumps({"complete": True, "observations": len(report["observations"]),
                      "vectors": sum(r["vector"] is not None for r in report["observations"]),
                      "reference_candidates": sum(r["reference_ok"] for r in report["observations"]),
                      "initial_reference": report["initial_reference"]}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-report", type=Path, required=True)
    parser.add_argument("--input", type=Path, default=ROOT / "video")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    build(parser.parse_args())


if __name__ == "__main__":
    main()
