"""Offline recording analysis. Never runs attendance workers or sends API events."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.offline_video import crop_bounds, export_gallery, latency_summary, sample_due, write_report


def load_gallery(path):
    import numpy as np
    payload = json.loads(path.read_text(encoding="utf-8"))
    names = payload["identities"]
    vectors = np.asarray(payload["vectors"], dtype=np.float32)
    if names:
        if vectors.shape != (len(names), 512) or not np.isfinite(vectors).all():
            raise ValueError("Invalid gallery vectors")
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        if (norms <= 0).any():
            raise ValueError("Zero-norm gallery vector")
        vectors = vectors / norms
    for key in ("threshold", "eduschool_threshold"):
        if not 0 < float(payload[key]) <= 1:
            raise ValueError("Invalid recognition threshold")
    return payload, names, vectors


def prepare_media(output, executable):
    report = json.loads((output / "results.json").read_text(encoding="utf-8"))
    for video in report["videos"]:
        source = output / video["video"]
        target = source.with_name(source.stem + "-browser.mp4")
        subprocess.run([executable, "-hide_banner", "-loglevel", "error", "-nostdin", "-n", "-i", str(source),
                        "-map", "0:v:0", "-vf", "scale=1280:-2", "-c:v", "libx264", "-preset", "fast",
                        "-threads", "2", "-crf", "23", "-pix_fmt", "yuv420p", "-an", "-movflags", "+faststart",
                        str(target)], check=True)
        video["video"] = target.relative_to(output).as_posix()
        print(json.dumps({"browser_preview": video["name"]}), flush=True)
    (output / "results.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    write_report(report, output / "index.html", ROOT / "tools/templates/video_report.html")


def match_crop(crop, recognizer, names, vectors, threshold, edu_threshold):
    import numpy as np
    from core.ai.engine import _choose_best_match
    start = time.perf_counter()
    embedding = recognizer.get_embedding(crop, target_center=True)
    elapsed = (time.perf_counter() - start) * 1000
    if embedding is None:
        return {"status": "no_embedding"}, elapsed
    embedding = np.asarray(embedding, dtype=np.float32)
    norm = np.linalg.norm(embedding)
    if embedding.shape != (512,) or not np.isfinite(embedding).all() or norm <= 0:
        return {"status": "no_embedding"}, elapsed
    if not names:
        return {"status": "no_gallery"}, elapsed
    best, score = _choose_best_match(names, vectors @ (embedding / norm), threshold, edu_threshold)
    identity = names[best]
    gate = max(threshold, edu_threshold) if identity["person_id"].startswith("edu:") else threshold
    if score > gate:
        return {"status": "matched", "name": identity["name"], "person_id": identity["person_id"],
                "person_type": identity["person_type"]}, elapsed
    return {"status": "unknown"}, elapsed


def analyze_file(path, index, output, model, recognizer, gallery, names, vectors, args):
    import cv2
    from core.ai.engine import MIN_FACE_SIZE_PX
    from core.ai.runtime import limit_torch_threads

    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        raise ValueError(f"Cannot open recording: {path.name}")
    fps = capture.get(cv2.CAP_PROP_FPS)
    if not math.isfinite(fps) or fps <= 0:
        capture.release()
        raise ValueError(f"Invalid frame rate: {path.name}")
    if args.sample_fps > fps:
        capture.release()
        raise ValueError("sample-fps cannot exceed source frame rate")
    expected_frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    width, height = int(capture.get(3)), int(capture.get(4))
    samples, detection_times, face_times = [], [], []
    counters = Counter()
    frame_index, next_sample = 0, 0
    started = time.perf_counter()
    try:
        while capture.grab():
            if sample_due(frame_index, fps, args.sample_fps, next_sample):
                ok, frame = capture.retrieve()
                if not ok:
                    raise ValueError(f"Decode failure at frame {frame_index}: {path.name}")
                timestamp = frame_index / fps
                tick = time.perf_counter()
                result = model.predict(frame, device=args.yolo_device, imgsz=args.imgsz, verbose=False)[0]
                detection_times.append((time.perf_counter() - tick) * 1000)
                limit_torch_threads()
                boxes = result.boxes.xyxy.cpu().numpy().astype(int) if result.boxes is not None else []
                faces = []
                for raw_box in boxes:
                    x1, y1, x2, y2 = map(int, raw_box)
                    x1, y1, x2, y2 = max(0, x1), max(0, y1), min(width, x2), min(height, y2)
                    box = [x1, y1, x2, y2]
                    face = {"box": box}
                    if min(x2 - x1, y2 - y1) < MIN_FACE_SIZE_PX:
                        face["status"] = "small"
                    else:
                        cx1, cy1, cx2, cy2 = crop_bounds(box, width, height)
                        outcome, elapsed = match_crop(frame[cy1:cy2, cx1:cx2], recognizer, names, vectors,
                                                      gallery["threshold"], gallery["eduschool_threshold"])
                        face.update(outcome)
                        face_times.append(elapsed)
                    counters[face["status"]] += 1
                    faces.append(face)
                sample = {"time": round(timestamp, 4), "frame": frame_index, "faces": faces,
                          "detection_ms": round(detection_times[-1], 1)}
                # Exact analysed frames remain reviewable even if a browser cannot decode the source codec.
                scale = min(1., 960 / width)
                preview = cv2.resize(frame, (round(width * scale), round(height * scale)))
                image_name = f"media/{index:02d}-{next_sample:05d}.jpg"
                if not cv2.imwrite(str(output / image_name), preview, [cv2.IMWRITE_JPEG_QUALITY, 78]):
                    raise OSError("Could not save analysed frame")
                sample["image"] = image_name
                samples.append(sample)
                next_sample += 1
                if next_sample % 50 == 0:
                    print(json.dumps({"video": path.name, "seconds": round(timestamp, 1),
                                      "samples": next_sample, "faces": sum(counters.values())}), flush=True)
            frame_index += 1
    finally:
        capture.release()
    if not samples:
        raise ValueError(f"No frames decoded: {path.name}")
    elapsed = time.perf_counter() - started
    if args.ffmpeg:
        media_name = f"media/{index:02d}.mp4"
        subprocess.run([args.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(path),
                        "-map", "0:v:0", "-vf", "scale=1280:-2", "-c:v", "libx264", "-preset", "fast",
                        "-crf", "23", "-pix_fmt", "yuv420p", "-an", "-movflags", "+faststart",
                        str(output / media_name)], check=True)
    else:
        media_name = f"media/{index:02d}{path.suffix.lower()}"
        shutil.copy2(path, output / media_name)
    return {"name": path.name, "video": media_name, "width": width, "height": height,
            "fps": round(fps, 3), "duration": round(frame_index / fps, 3), "decoded_frames": frame_index,
            "expected_frames": expected_frames, "decode_complete": abs(expected_frames - frame_index) <= 1,
            "samples": samples, "counts": dict(counters), "elapsed_seconds": round(elapsed, 2),
            "processed_fps": round(len(samples) / elapsed, 2),
            "detection_latency": latency_summary(detection_times), "face_latency": latency_summary(face_times),
            "face_device": "cuda" if recognizer.using_cuda else "cpu"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export-gallery", type=Path)
    parser.add_argument("--render-only", action="store_true", help="Rebuild HTML from existing results.json")
    parser.add_argument("--prepare-media", action="store_true", help="Create H.264 previews from existing results")
    parser.add_argument("--gallery", type=Path)
    parser.add_argument("--input", type=Path, default=ROOT / "video")
    parser.add_argument("--output", type=Path, default=ROOT / "data/exports/video-review")
    parser.add_argument("--sample-fps", type=float, default=5)
    parser.add_argument("--imgsz", type=int, choices=(640, 960), default=960)
    parser.add_argument("--device", choices=("cpu", "auto", "cuda"), default="cpu")
    parser.add_argument("--ffmpeg", help="Optional ffmpeg executable for browser-compatible silent H.264 previews")
    args = parser.parse_args()
    if args.prepare_media:
        executable = args.ffmpeg or shutil.which("ffmpeg")
        if not executable:
            parser.error("ffmpeg is required to prepare browser previews")
        prepare_media(args.output, executable)
        return
    if args.render_only:
        report = json.loads((args.output / "results.json").read_text(encoding="utf-8"))
        write_report(report, args.output / "index.html", ROOT / "tools/templates/video_report.html")
        return
    if args.export_gallery:
        print(json.dumps(export_gallery(args.export_gallery)))
        return
    if not args.gallery or not 0 < args.sample_fps <= 30:
        parser.error("--gallery is required; sample-fps must be > 0 and <= 30")
    files = sorted(path for path in args.input.iterdir() if path.suffix.lower() in {".mp4", ".avi", ".mkv", ".mov"})
    if not files:
        parser.error("No recordings found")
    if args.output.exists() and any(args.output.iterdir()):
        parser.error("Output must be empty to preserve existing reviews")
    (args.output / "media").mkdir(parents=True, exist_ok=True)
    os.environ["VISION_OFFICE_DEVICE"] = args.device
    os.environ.setdefault("YOLO_AUTOINSTALL", "false")
    from core.ai.engine import detector_model_path
    from core.ai.recognizer import FaceRecognizer
    from core.ai.runtime import torch_device
    from ultralytics import YOLO
    import numpy as np

    gallery, names, vectors = load_gallery(args.gallery)
    device, fallback = torch_device()
    if args.device == "cuda" and device != "cuda":
        raise RuntimeError(f"Requested CUDA is unavailable: {fallback}")
    args.yolo_device = 0 if device == "cuda" else "cpu"
    model = YOLO(detector_model_path())
    model.predict(np.zeros((args.imgsz, args.imgsz, 3), dtype=np.uint8), device=args.yolo_device,
                  imgsz=args.imgsz, verbose=False)
    recognizer = FaceRecognizer(use_cuda=device == "cuda")
    recognizer.sanity_check()
    report = {"created_at": datetime.now(timezone.utc).isoformat(), "gallery": gallery["summary"],
              "gallery_created_at": gallery["created_at"], "sample_fps": args.sample_fps,
              "imgsz": args.imgsz, "device": device, "fallback": fallback,
              "threshold": gallery["threshold"], "eduschool_threshold": gallery["eduschool_threshold"],
              "videos": []}
    for index, path in enumerate(files):
        result = analyze_file(path, index, args.output, model, recognizer, gallery, names, vectors, args)
        report["videos"].append(result)
        (args.output / "results.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        write_report(report, args.output / "index.html", ROOT / "tools/templates/video_report.html")
        print(json.dumps({"completed": path.name, "counts": result["counts"],
                          "detection_latency": result["detection_latency"]}), flush=True)
    print(f"REPORT: {args.output / 'index.html'}", flush=True)


if __name__ == "__main__":
    main()
