"""Paired offline full-frame/ROI experiment; consumes a gallery snapshot, never a DB."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.offline_video import crop_bounds, latency_summary, sample_due, write_report
from tools.analyze_videos import load_gallery, match_crop


def validate_roi(roi, width, height):
    x1, y1, x2, y2 = roi
    if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
        raise ValueError("ROI must be a nonempty rectangle inside the original video")
    return list(roi)


def scoped_boxes(raw_boxes, roi, cropped):
    """Compare only boxes fully inside the same zone; exclude boundary truncation."""
    x1, y1, x2, y2 = roi
    boxes = []
    for raw in raw_boxes:
        a, b, c, d = map(int, raw)
        if cropped:
            a, b, c, d = a + x1, b + y1, c + x1, d + y1
        if x1 + 2 <= a < c <= x2 - 2 and y1 + 2 <= b < d <= y2 - 2:
            boxes.append([a, b, c, d])
    return boxes


def analyze_variant(frame, roi, cropped, model, recognizer, gallery, names, vectors, device, imgsz):
    from core.ai.engine import MIN_FACE_SIZE_PX
    from core.ai.runtime import limit_torch_threads

    x1, y1, x2, y2 = roi
    source = frame[y1:y2, x1:x2].copy() if cropped else frame
    started = time.perf_counter()
    prediction = model.predict(source, device=device, imgsz=imgsz, verbose=False)[0]
    detection_ms = (time.perf_counter() - started) * 1000
    limit_torch_threads()
    raw = prediction.boxes.xyxy.cpu().numpy() if prediction.boxes is not None else []
    faces, face_times = [], []
    for box in scoped_boxes(raw, roi, cropped):
        face = {"box": box.copy()}
        if min(box[2] - box[0], box[3] - box[1]) < MIN_FACE_SIZE_PX:
            face["status"] = "small"
        else:
            a, b, c, d = crop_bounds(box, frame.shape[1], frame.shape[0])
            # Both variants run FaceID on original pixels with identical padding.
            outcome, elapsed = match_crop(frame[b:d, a:c], recognizer, names, vectors,
                                          gallery["threshold"], gallery["eduschool_threshold"])
            face.update(outcome)
            face_times.append(elapsed)
        if cropped:
            face["box"] = [box[0] - x1, box[1] - y1, box[2] - x1, box[3] - y1]
        faces.append(face)
    return faces, detection_ms, face_times, (time.perf_counter() - started) * 1000


def metadata(path):
    import cv2
    cap = cv2.VideoCapture(str(path))
    try:
        if not cap.isOpened():
            raise ValueError(f"Cannot open video: {path.name}")
        result = dict(width=int(cap.get(3)), height=int(cap.get(4)),
                      fps=cap.get(cv2.CAP_PROP_FPS), frames=int(cap.get(cv2.CAP_PROP_FRAME_COUNT)))
        if not math.isfinite(result["fps"]) or result["fps"] <= 0 or result["frames"] <= 0:
            raise ValueError("Video requires a finite frame rate and frame count")
        return result
    finally:
        cap.release()


def validate_preview(source, preview, aspect):
    if abs(source["frames"] - preview["frames"]) > 1 or abs(source["fps"] - preview["fps"]) > .01:
        raise ValueError("Preview must preserve original frame count and frame rate")
    if abs(preview["width"] / preview["height"] - aspect) > .01:
        raise ValueError("Preview aspect ratio does not match its variant")


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def run(args):
    import cv2
    import numpy as np
    from core.ai.engine import detector_model_path
    from core.ai.recognizer import FaceRecognizer
    from core.ai.runtime import torch_device
    from ultralytics import YOLO

    meta = metadata(args.input)
    roi = validate_roi(args.roi, meta["width"], meta["height"])
    if not 0 < args.sample_fps <= min(30, meta["fps"]):
        raise ValueError("sample-fps must be positive and no higher than the source FPS or 30")
    rw, rh = roi[2] - roi[0], roi[3] - roi[1]
    validate_preview(meta, metadata(args.preview), meta["width"] / meta["height"])
    validate_preview(meta, metadata(args.roi_preview), rw / rh)
    if args.output.exists():
        raise ValueError("Choose a new output directory; existing reports are preserved")
    gallery, names, vectors = load_gallery(args.gallery)
    os.environ["VISION_OFFICE_DEVICE"] = args.device
    os.environ.setdefault("YOLO_AUTOINSTALL", "false")
    device, fallback = torch_device()
    if args.device == "cuda" and device != "cuda":
        raise RuntimeError(f"Requested CUDA unavailable: {fallback}")
    yolo_device = 0 if device == "cuda" else "cpu"
    model_path = Path(detector_model_path())
    model = YOLO(str(model_path))
    # Warm both detector input shapes before paired timing.
    for width, height in ((meta["width"], meta["height"]), (rw, rh)):
        model.predict(np.zeros((height, width, 3), dtype=np.uint8), device=yolo_device,
                      imgsz=args.imgsz, verbose=False)
    recognizer = FaceRecognizer(use_cuda=device == "cuda")
    recognizer.sanity_check()
    (args.output / "media").mkdir(parents=True)
    shutil.copy2(args.preview, args.output / "media/full.mp4")
    shutil.copy2(args.roi_preview, args.output / "media/roi.mp4")
    report = {"created_at": datetime.now(timezone.utc).isoformat(), "gallery": gallery["summary"],
              "gallery_created_at": gallery["created_at"], "sample_fps": args.sample_fps,
              "imgsz": args.imgsz, "device": device, "fallback": fallback,
              "threshold": gallery["threshold"], "eduschool_threshold": gallery["eduschool_threshold"],
              "comparison": {"roi": roi, "source": args.input.name, "source_sha256": digest(args.input),
                             "gallery_sha256": digest(args.gallery), "detector_sha256": digest(model_path),
                             "boundary_margin": 2, "timing_order": "alternating", "rows": []}, "videos": []}
    timings = [{"detect": [], "face": [], "total": []} for _ in range(2)]
    for i, label in enumerate(("A · Полный кадр", "B · Зона прохода")):
        report["videos"].append({"name": label, "video": f"media/{'roi' if i else 'full'}.mp4",
                                 "width": rw if i else meta["width"], "height": rh if i else meta["height"],
                                 "fps": meta["fps"], "duration": meta["frames"] / meta["fps"],
                                 "samples": [], "counts": {}, "roi": None if i else roi})
    capture = cv2.VideoCapture(str(args.input))
    index, sampled = 0, 0
    try:
        while capture.grab():
            if sample_due(index, meta["fps"], args.sample_fps, sampled):
                ok, frame = capture.retrieve()
                if not ok:
                    raise RuntimeError(f"Decode failed at frame {index}")
                # Alternate order to reduce systematic first/second-run timing bias.
                for variant in ((0, 1) if sampled % 2 == 0 else (1, 0)):
                    faces, det, face_ms, total = analyze_variant(frame, roi, bool(variant), model, recognizer,
                                                               gallery, names, vectors, yolo_device, args.imgsz)
                    crop = frame[roi[1]:roi[3], roi[0]:roi[2]] if variant else frame
                    scale = min(1, 1120 / crop.shape[1])
                    preview = cv2.resize(crop, (round(crop.shape[1] * scale), round(crop.shape[0] * scale)))
                    image = f"media/{variant}-{sampled:05d}.jpg"
                    if not cv2.imwrite(str(args.output / image), preview, [cv2.IMWRITE_JPEG_QUALITY, 90]):
                        raise OSError("Failed to write comparison frame")
                    report["videos"][variant]["samples"].append({"frame": index, "time": round(index / meta["fps"], 4),
                                                                 "image": image, "faces": faces, "detection_ms": round(det, 1)})
                    timings[variant]["detect"].append(det)
                    timings[variant]["face"].extend(face_ms)
                    timings[variant]["total"].append(total)
                sampled += 1
                if sampled % 50 == 0:
                    print(json.dumps({"paired_frames": sampled, "seconds": round(index / meta["fps"], 1)}), flush=True)
            index += 1
    finally:
        capture.release()
    if not sampled:
        raise RuntimeError("No frames sampled")
    for i, video in enumerate(report["videos"]):
        faces = [f for s in video["samples"] for f in s["faces"]]
        counts = dict(Counter(f["status"] for f in faces))
        total = sum(timings[i]["total"]) / 1000
        video.update(counts=counts, decoded_frames=index, expected_frames=meta["frames"],
                     decode_complete=abs(index - meta["frames"]) <= 1,
                     duration=index / meta["fps"], elapsed_seconds=round(total, 2),
                     processed_fps=round(sampled / total, 2), detection_latency=latency_summary(timings[i]["detect"]),
                     face_latency=latency_summary(timings[i]["face"]), face_device="cuda" if recognizer.using_cuda else "cpu")
        report["comparison"]["rows"].append({"variant": video["name"], "observations": len(faces), **counts,
                                              "predicted_profiles": len({f["person_id"] for f in faces if f["status"] == "matched"}),
                                              "pipeline_p95": latency_summary(timings[i]["total"])["p95"]})
    (args.output / "results.json").write_text(json.dumps(report, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    write_report(report, args.output / "index.html", ROOT / "tools/templates/video_report.html")
    print(json.dumps(report["comparison"]["rows"], ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--gallery", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--roi", type=int, nargs=4, required=True, metavar=("X1", "Y1", "X2", "Y2"))
    parser.add_argument("--preview", type=Path, required=True, help="Full-frame H.264 preview from the same source")
    parser.add_argument("--roi-preview", type=Path, required=True, help="H.264 crop with the specified exact ROI")
    parser.add_argument("--sample-fps", type=float, default=5)
    parser.add_argument("--imgsz", type=int, choices=(640, 960), default=960)
    parser.add_argument("--device", choices=("cpu", "auto", "cuda"), default="cpu")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
