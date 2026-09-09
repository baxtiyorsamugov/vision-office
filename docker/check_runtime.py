"""Bounded real-model probe; no RTSP, database writes, photos or ERP calls."""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
from pathlib import Path
import queue
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def exercise(kind, device, image_size, results, release):
    try:
        import numpy as np
        os.environ["VISION_OFFICE_DEVICE"] = "cpu" if device == "cpu" else "auto"
        # Probes bypass entrypoint.sh; separate settings prevent startup write races.
        import tempfile
        os.environ["YOLO_CONFIG_DIR"] = str(Path(tempfile.gettempdir()) / f"vision-probe-{os.getpid()}")
        (Path(os.environ["YOLO_CONFIG_DIR"]) / "Ultralytics").mkdir(parents=True, exist_ok=True)
        if kind == "yolo":
            from core.ai.engine import detector_model_path
            from core.ai.runtime import limit_torch_threads, torch_device
            from ultralytics import YOLO
            import torch
            selected, reason = torch_device()
            if device == "cuda" and selected != "cuda":
                raise RuntimeError(reason)
            path = detector_model_path()
            if not Path(path).is_file():
                raise RuntimeError("YOLO model is missing")
            model = YOLO(path)
            frame = np.zeros((image_size, image_size, 3), dtype=np.uint8)
            def run():
                model.track(frame, persist=True, tracker="bytetrack.yaml", verbose=False,
                            imgsz=image_size, device=0 if selected == "cuda" else "cpu")
                limit_torch_threads()
                if selected == "cuda":
                    torch.cuda.synchronize()
            actual = selected
        else:
            from core.ai.recognizer import FaceRecognizer
            for name in ("det_10g.onnx", "w600k_r50.onnx"):
                if not (ROOT / "models/insightface/models/buffalo_l" / name).is_file():
                    raise RuntimeError(f"Missing FaceID model: {name}")
            model = FaceRecognizer()
            actual = "cuda" if model.using_cuda else "cpu"
            if device == "cuda" and not model.using_cuda:
                raise RuntimeError(model.fallback_reason or "FaceID CUDA unavailable")
            run = model.sanity_check
        run()
        timings = []
        for _ in range(5):
            started = time.perf_counter()
            run()
            timings.append((time.perf_counter() - started) * 1000)
        results.put({"kind": kind, "device": actual, "ok": True,
                     "median_ms": round(float(np.median(timings)), 2),
                     "p95_ms": round(float(np.percentile(timings, 95)), 2)})
        # Keep every model/context resident until all configured cameras pass.
        release.wait(600)
    except Exception as error:
        results.put({"kind": kind, "ok": False, "error": str(error)[:500]})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", choices=("cpu", "cuda"), required=True)
    parser.add_argument("--timeout", type=int, default=240)
    args = parser.parse_args()
    from core.config import load_app_settings
    config = load_app_settings(ROOT / "config/settings.yaml")
    cameras = sum(camera.is_active for camera in config.cameras)
    if not cameras:
        print("No active cameras configured; nothing to validate.", file=sys.stderr)
        return 1
    size = max(640, int(config.ai.get("face_detection_imgsz", 960)))
    context = mp.get_context("spawn")
    results = context.Queue()
    release = context.Event()
    workers = []
    reports = []
    deadline = time.monotonic() + args.timeout
    try:
        for _ in range(cameras):
            for kind in ("yolo", "face"):
                process = context.Process(target=exercise, args=(kind, args.device, size, results, release))
                process.start()
                workers.append(process)
        while len(reports) < len(workers) and time.monotonic() < deadline:
            try:
                report = results.get(timeout=1)
                reports.append(report)
                print(json.dumps(report), flush=True)
                if not report["ok"]:
                    break
            except queue.Empty:
                if any(worker.exitcode not in (None, 0) for worker in workers):
                    break
        ok = len(reports) == len(workers) and all(report["ok"] for report in reports)
        print(json.dumps({"runtime_probe": "passed" if ok else "failed", "device": args.device,
                          "cameras": cameras, "reports": reports}), flush=True)
        return 0 if ok else 1
    finally:
        release.set()
        for worker in workers:
            worker.join(timeout=3)
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=3)
            if worker.is_alive():
                worker.kill()
                worker.join()


if __name__ == "__main__":
    raise SystemExit(main())
