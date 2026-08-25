"""Measure face-detection and face-recognition performance on a video source.

Examples:
    python benchmark_recognition.py test.mp4
    python benchmark_recognition.py rtsp://user:password@camera/Streaming/Channels/101
"""

import argparse
import statistics
import time

import cv2
import numpy as np
import onnxruntime as ort
import torch
from ultralytics import YOLO

from core.ai.recognizer import FaceRecognizer


def percentile(values, q):
    if not values:
        return 0.0
    return float(np.percentile(values, q))


def crop_face(frame, box):
    x1, y1, x2, y2 = map(int, box)
    width, height = x2 - x1, y2 - y1
    pad_x, pad_y = int(width * 0.20), int(height * 0.35)
    frame_h, frame_w = frame.shape[:2]
    face = frame[
        max(0, y1 - pad_y):min(frame_h, y2 + pad_y),
        max(0, x1 - pad_x):min(frame_w, x2 + pad_x),
    ]
    if face.size == 0:
        return None
    if face.shape[0] < 112 or face.shape[1] < 112:
        face = cv2.resize(face, (150, 150), interpolation=cv2.INTER_CUBIC)
    return face


def run_benchmark(source, model_path, max_frames, every_n_frames, max_faces_per_frame, skip_frames):
    model = YOLO(model_path)
    using_cuda = torch.cuda.is_available()
    if using_cuda:
        torch.backends.cudnn.benchmark = True
    yolo_options = {
        "device": 0 if using_cuda else "cpu",
        "quantize": 16 if using_cuda else None,
    }
    recognizer = FaceRecognizer()
    capture = cv2.VideoCapture(source)
    if not capture.isOpened():
        raise RuntimeError(f"Cannot open video source: {source}")

    detection_ms = []
    recognition_ms = []
    processed_frames = 0
    decoded_frames = 0
    detected_faces = 0
    embedding_count = 0
    started = time.perf_counter()

    try:
        for _ in range(skip_frames):
            ok, _ = capture.read()
            if not ok:
                raise RuntimeError("Video ended before --skip-frames")

        while processed_frames < max_frames:
            ok, frame = capture.read()
            if not ok:
                break
            decoded_frames += 1
            if decoded_frames % every_n_frames:
                continue

            detect_started = time.perf_counter()
            result = model.predict(frame, imgsz=640, conf=0.5, verbose=False, **yolo_options)[0]
            detection_ms.append((time.perf_counter() - detect_started) * 1000)
            processed_frames += 1

            boxes = result.boxes.xyxy.cpu().numpy() if result.boxes else []
            detected_faces += len(boxes)
            for box in boxes[:max_faces_per_frame]:
                face = crop_face(frame, box)
                if face is None:
                    continue
                recognition_started = time.perf_counter()
                embedding = recognizer.get_embedding(face)
                recognition_ms.append((time.perf_counter() - recognition_started) * 1000)
                embedding_count += embedding is not None
    finally:
        capture.release()

    elapsed = time.perf_counter() - started
    print("Recognition benchmark")
    print(f"source: {source}")
    print(f"frames: {processed_frames} processed / {decoded_frames} decoded")
    print(f"wall_fps: {processed_frames / elapsed:.2f}")
    print(f"faces: {detected_faces}, embeddings: {embedding_count}")
    print(f"torch_cuda_available: {torch.cuda.is_available()}")
    print(f"yolo_device: {'CUDA:0 (FP16)' if using_cuda else 'CPU'}")
    print(f"onnx_providers: {ort.get_available_providers()}")
    print(f"yolo_ms_avg: {statistics.fmean(detection_ms):.1f}")
    print(f"yolo_ms_p95: {percentile(detection_ms, 95):.1f}")
    print(f"yolo_ms_max: {max(detection_ms):.1f}")
    if recognition_ms:
        print(f"recognition_ms_avg: {statistics.fmean(recognition_ms):.1f}")
        print(f"recognition_ms_p95: {percentile(recognition_ms, 95):.1f}")
        print(f"recognition_ms_max: {max(recognition_ms):.1f}")
        print(f"embedding_success_rate: {embedding_count / len(recognition_ms):.1%}")
    else:
        print("recognition_ms_avg: n/a (no face crops)")


def parse_args():
    parser = argparse.ArgumentParser(description="Benchmark the recognition pipeline.")
    parser.add_argument("source", help="Video file path or RTSP URL")
    parser.add_argument("--model", default="yolov8n-face.pt", help="YOLO face model path")
    parser.add_argument("--frames", type=int, default=120, help="Number of frames to process")
    parser.add_argument("--every", type=int, default=1, help="Process every Nth decoded frame")
    parser.add_argument("--max-faces", type=int, default=2, help="Recognition attempts per processed frame")
    parser.add_argument("--skip-frames", type=int, default=0, help="Frames to discard before measuring")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.frames < 1 or args.every < 1 or args.max_faces < 1 or args.skip_frames < 0:
        raise SystemExit("--frames, --every and --max-faces must be positive; --skip-frames cannot be negative")
    run_benchmark(args.source, args.model, args.frames, args.every, args.max_faces, args.skip_frames)
