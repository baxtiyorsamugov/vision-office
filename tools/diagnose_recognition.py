"""Measure real camera inference without writing events or invoking any ERP API."""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
from ultralytics import YOLO

from core.ai.engine import detector_model_path
from core.ai.recognizer import FaceRecognizer
from core.ai.runtime import limit_torch_threads, torch_device
from core.config import load_app_settings
from core.video.streamer import VideoStream


def diagnose(camera_id, samples=20, image_size=640):
    settings = load_app_settings()
    camera = next((item for item in settings.cameras if item.id == camera_id), None)
    if camera is None:
        raise ValueError("Camera ID is not configured")
    device, fallback = torch_device()
    model = YOLO(detector_model_path())
    options = {"device": 0 if device == "cuda" else "cpu", "imgsz": image_size, "verbose": False}
    model.predict(np.zeros((image_size, image_size, 3), dtype=np.uint8), **options)
    limit_torch_threads()
    recognizer = FaceRecognizer()
    recognizer.sanity_check()
    capture = VideoStream(camera.rtsp_url).start()
    detection_ms, face_ms = [], []
    faces_seen = embeddings = 0
    deadline = time.monotonic() + 45
    started = time.monotonic()
    try:
        while len(detection_ms) < samples and time.monotonic() < deadline:
            frame = capture.read()
            if frame is None:
                time.sleep(0.01)
                continue
            tick = time.perf_counter()
            result = model.predict(frame, **options)[0]
            detection_ms.append((time.perf_counter() - tick) * 1000)
            limit_torch_threads()
            boxes = result.boxes.xyxy.cpu().numpy() if result.boxes is not None else []
            faces_seen += len(boxes)
            for box in boxes[:3]:
                x1, y1, x2, y2 = map(int, box)
                width, height = x2 - x1, y2 - y1
                if min(width, height) < 48:
                    continue
                px, py = int(width * .2), int(height * .35)
                crop = frame[max(0, y1-py):min(frame.shape[0], y2+py), max(0, x1-px):min(frame.shape[1], x2+px)]
                if not crop.size:
                    continue
                tick = time.perf_counter()
                embedding = recognizer.get_embedding(crop, target_center=True)
                face_ms.append((time.perf_counter() - tick) * 1000)
                embeddings += embedding is not None
        stream = capture.stats()
    finally:
        capture.stop()
    def stats(values):
        return {"p50_ms": round(float(np.percentile(values, 50)), 1),
                "p95_ms": round(float(np.percentile(values, 95)), 1)} if values else None
    return {"camera_id": camera_id, "device": device, "fallback": fallback,
            "face_device": "cuda" if recognizer.using_cuda else "cpu", "image_size": image_size,
            "samples": len(detection_ms), "elapsed_seconds": round(time.monotonic() - started, 2),
            "detection": stats(detection_ms), "faceid": stats(face_ms),
            "faces_seen": faces_seen, "face_crops": len(face_ms), "usable_embeddings": embeddings,
            "capture_fps": stream["capture_fps"], "frame_age_ms": stream["frame_age_ms"],
            "note": "Latency sample only, not an accuracy test. No events or images saved; no API calls."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--camera", required=True)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--imgsz", type=int, choices=(640, 960), default=640)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.samples <= 200:
        parser.error("samples must be between 1 and 200")
    report = diagnose(args.camera, args.samples, args.imgsz)
    payload = json.dumps(report, indent=2)
    print(payload)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    if not report["samples"]:
        raise SystemExit(2)
