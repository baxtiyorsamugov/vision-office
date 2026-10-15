"""Isolated FaceID experiments on original recordings and frozen YOLO observations.

No camera workers, database connections, enrollment or delivery services are used.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.offline_video import crop_bounds, latency_summary, write_report
from tools.analyze_videos import load_gallery, match_crop
from tools.compare_video_roi import digest, metadata

VARIANTS = [
    {"id": "base", "name": "A · Текущий 320"},
    {"id": "target", "name": "B · Центр рамки YOLO"},
    {"id": "retry", "name": "C · Повтор FaceID"},
    {"id": "compact", "name": "D · FaceID 160"},
]


def local_child(directory, name):
    path = (Path(directory) / name).resolve()
    if path.parent != Path(directory).resolve():
        raise ValueError("Recording name must be a filename, not a path")
    return path


def expanded_bounds(box, width, height):
    a, b, c, d = box
    px, py = int((c-a)*.4), int((d-b)*.55)
    return max(0, a-px), max(0, b-py), min(width, c+px), min(height, d+py)


def choose_face(faces, center, target=None):
    """Reject ambiguous crops instead of choosing a larger neighbouring face."""
    import numpy as np
    if not faces:
        return None, "no_detection"
    candidates = [f for f in faces if f.bbox[0] <= center[0] <= f.bbox[2]
                  and f.bbox[1] <= center[1] <= f.bbox[3]]
    if len(candidates) != 1:
        return None, "ambiguous" if len(candidates) > 1 else "center_miss"
    face = candidates[0]
    if target is not None:
        a, b, c, d = map(float, face.bbox)
        x1, y1, x2, y2 = target
        overlap = max(0, min(c, x2)-max(a, x1))*max(0, min(d, y2)-max(b, y1))
        union = max(0, c-a)*max(0, d-b) + (x2-x1)*(y2-y1) - overlap
        if union <= 0 or overlap / union < .25:
            return None, "overlap_miss"
    vector = np.asarray(face.embedding, dtype=np.float32)
    if vector.shape != (512,) or not np.isfinite(vector).all() or np.linalg.norm(vector) <= 0:
        return None, "invalid_vector"
    return vector, "usable"


def relative_target(box, bounds):
    a, b, _, _ = bounds
    target = [box[0]-a, box[1]-b, box[2]-a, box[3]-b]
    return ((target[0]+target[2])/2, (target[1]+target[3])/2), target


def classify(vector, reason, elapsed, route, gallery, names, vectors):
    if vector is None:
        outcome = {"status": "no_embedding"}
    else:
        start = time.perf_counter()
        outcome, _ = match_crop(None, SimpleNamespace(get_embedding=lambda *a, **kw: vector),
                                         names, vectors, gallery["threshold"], gallery["eduschool_threshold"])
        elapsed += (time.perf_counter()-start)*1000
    return {**outcome, "reason": reason, "route": route, "ms": round(elapsed, 2)}


def evaluate(frame, box, app320, app160, gallery, names, vectors, order=0):
    height, width = frame.shape[:2]
    bounds = crop_bounds(box, width, height)
    a, b, c, d = bounds
    crop = frame[b:d, a:c]
    center, target = relative_target(box, bounds)
    measured = {}
    # Alternate execution order. Shared raw results avoid recomputing identical work.
    for key, app in (("320", app320), ("160", app160)) if order % 2 == 0 else (("160", app160), ("320", app320)):
        start = time.perf_counter()
        faces = app.get(crop)
        measured[key] = (faces, (time.perf_counter()-start)*1000)
    faces, ms320 = measured["320"]
    vector, reason = choose_face(faces, (crop.shape[1]/2, crop.shape[0]/2))
    base = classify(vector, reason, ms320, "original320", gallery, names, vectors)
    vector, reason = choose_face(faces, center, target)
    targeted = classify(vector, reason, ms320, "target320", gallery, names, vectors)
    faces160, ms160 = measured["160"]
    vector160, reason160 = choose_face(faces160, center, target)
    compact = classify(vector160, reason160, ms160, "target160", gallery, names, vectors)
    retried = dict(targeted)
    if vector is None:
        wide = expanded_bounds(box, width, height)
        a, b, c, d = wide
        start = time.perf_counter()
        wide_faces = app320.get(frame[b:d, a:c])
        extra_ms = (time.perf_counter()-start)*1000
        wide_center, wide_target = relative_target(box, wide)
        wide_vector, wide_reason = choose_face(wide_faces, wide_center, wide_target)
        retried = classify(wide_vector, wide_reason, ms320+extra_ms, "wide320", gallery, names, vectors)
        if wide_vector is None:
            retried = {**compact, "ms": round(ms320+extra_ms+compact["ms"], 2), "route": "retry160"}
    return {"base": base, "target": targeted, "retry": retried, "compact": compact}


def summarize(rows):
    result = []
    for variant in VARIANTS:
        key = variant["id"]
        outcomes = [r["outcomes"][key] for r in rows]
        counts = Counter(o["status"] for o in outcomes)
        changes = Counter()
        for r in rows:
            base, other = r["outcomes"]["base"], r["outcomes"][key]
            bm, om = base["status"] == "matched", other["status"] == "matched"
            changes["new"] += om and not bm
            changes["lost"] += bm and not om
            changes["identity_changed"] += bm and om and base["person_id"] != other["person_id"]
        result.append({**variant, "count": len(rows), "counts": dict(counts), **changes,
                       "latency": latency_summary([o["ms"] for o in outcomes]),
                       "reasons": dict(Counter(o["reason"] for o in outcomes))})
    return result


def save(report, output):
    report["summary"] = summarize(report["observations"])
    temporary = output / "results.tmp"
    temporary.write_text(json.dumps(report, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    temporary.replace(output / "results.json")
    write_report(report, output / "index.html", ROOT / "tools/templates/faceid_lab.html")


def prepare_media(args):
    source = json.loads(args.source_report.read_text(encoding="utf-8"))
    args.output.mkdir(parents=True, exist_ok=False)
    manifest = []
    for i, video in enumerate(source["videos"]):
        path = local_child(args.input, video["name"])
        meta = metadata(path)
        target = args.output / f"{i:02d}.mp4"
        fps = str(meta["fps"])
        subprocess.run([args.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-n", "-i", str(path),
                        "-map", "0:v:0", "-vf", f"setpts=N/({fps}*TB),scale=1280:-2", "-r", fps,
                        "-fps_mode", "cfr", "-c:v", "libx264", "-threads", "2", "-preset", "fast", "-crf", "22",
                        "-pix_fmt", "yuv420p", "-an", "-movflags", "+faststart", str(target)], check=True)
        preview = metadata(target)
        if preview["frames"] != meta["frames"] or abs(preview["fps"]-meta["fps"]) > .001:
            raise ValueError("Preview changed analysis timeline")
        manifest.append({"name": path.name, "file": target.name, "sha256": digest(path), **meta})
        print(json.dumps({"preview_ready": i+1}), flush=True)
    (args.output / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def attach_media(args):
    report = json.loads((args.output / "results.json").read_text(encoding="utf-8"))
    manifest = json.loads((args.preview_dir / "manifest.json").read_text(encoding="utf-8"))
    lookup = {v["name"]: v for v in manifest}
    copies = []
    for i, video in enumerate(report["videos"]):
        item = lookup[video["name"]]
        source = local_child(args.preview_dir, item["file"])
        meta = metadata(source)
        if item["sha256"] != video["sha256"] or meta["frames"] != video["frames"] or abs(meta["fps"]-video["fps"]) > .001:
            raise ValueError("Preview provenance/timing mismatch")
        destination = args.output / "media" / f"video-{i:02d}.mp4"
        if destination.exists():
            raise ValueError("Preview already attached")
        copies.append((source, destination, video))
    for source, destination, video in copies:
        shutil.copy2(source, destination)
        video["video"] = destination.relative_to(args.output).as_posix()
    save(report, args.output)


def run(args):
    import cv2
    from core.ai.recognizer import FaceRecognizer, INSIGHTFACE_MODEL_ROOT

    if args.output.exists():
        raise ValueError("Use a new output directory to preserve previous experiments")
    source = json.loads(args.source_report.read_text(encoding="utf-8"))
    gallery, names, vectors = load_gallery(args.gallery)
    if source["gallery_created_at"] != gallery["created_at"]:
        raise ValueError("Use the gallery snapshot belonging to this source report")
    os.environ["VISION_OFFICE_DEVICE"] = args.device
    cv2.setNumThreads(1)
    models = [FaceRecognizer(det_size=(s, s), use_cuda=args.device == "cuda") for s in (320, 160)]
    if args.device == "cuda" and not all(m.using_cuda for m in models):
        raise RuntimeError("CUDA requested but a model fell back to CPU")
    for m in models:
        m.sanity_check()
    (args.output / "media").mkdir(parents=True)
    pack = INSIGHTFACE_MODEL_ROOT / "models/buffalo_l"
    report = {"created_at": datetime.now(timezone.utc).isoformat(), "complete": False,
              "variants": VARIANTS, "device": args.device, "sample_fps": source["sample_fps"],
              "threshold": gallery["threshold"], "eduschool_threshold": gallery["eduschool_threshold"],
              "gallery": gallery["summary"], "gallery_created_at": gallery["created_at"],
              "provenance": {"gallery_sha256": digest(args.gallery), "detections_sha256": digest(args.source_report),
                             "models": {p.name: digest(p) for p in (pack / "det_10g.onnx", pack / "w600k_r50.onnx")}},
              "videos": [], "observations": [], "skipped_small": 0}
    for vi, old in enumerate(source["videos"]):
        path = local_child(args.input, old["name"])
        meta = metadata(path)
        if (meta["width"] != old["width"] or meta["height"] != old["height"]
                or meta["frames"] != old["decoded_frames"] or abs(meta["fps"]-old["fps"]) > .001):
            raise ValueError("Source recording does not match frozen detections")
        samples = {s["frame"]: (si, s) for si, s in enumerate(old["samples"])}
        if len(samples) != len(old["samples"]) or any(f < 0 or f >= meta["frames"] for f in samples):
            raise ValueError("Invalid frozen sample indices")
        report["videos"].append({"name": path.name, **meta, "sha256": digest(path), "video": None})
        capture = cv2.VideoCapture(str(path))
        frame_index, seen = 0, 0
        try:
            while capture.grab():
                if frame_index in samples:
                    si, sample = samples[frame_index]
                    ok, frame = capture.retrieve()
                    if not ok:
                        raise RuntimeError("Recording decode failed")
                    seen += 1
                    eligible = []
                    for fi, face in enumerate(sample["faces"]):
                        a, b, c, d = face["box"]
                        if not (0 <= a < c <= meta["width"] and 0 <= b < d <= meta["height"]):
                            raise ValueError("Invalid source detection box")
                        if min(c-a, d-b) < 48:
                            report["skipped_small"] += 1
                        else:
                            eligible.append((fi, face))
                    if eligible:
                        context_path = f"media/context-{vi}-{si}.jpg"
                        scale = min(1, 1280/frame.shape[1])
                        context = cv2.resize(frame, (round(frame.shape[1]*scale), round(frame.shape[0]*scale)))
                        if not cv2.imwrite(str(args.output / context_path), context):
                            raise OSError("Could not save context frame")
                        for fi, face in eligible:
                            box = list(map(int, face["box"]))
                            results = evaluate(frame, box, models[0].app, models[1].app, gallery, names, vectors,
                                               order=len(report["observations"]))
                            a, b, c, d = crop_bounds(box, frame.shape[1], frame.shape[0])
                            image = f"media/face-{vi}-{si}-{fi}.jpg"
                            if not cv2.imwrite(str(args.output / image), frame[b:d, a:c], [cv2.IMWRITE_JPEG_QUALITY, 95]):
                                raise OSError("Could not save crop")
                            report["observations"].append({"id": f"{vi}:{si}:{fi}", "video": vi, "frame": frame_index,
                                                           "time": frame_index/meta["fps"], "box": box, "image": image,
                                                           "context": context_path, "outcomes": results})
                frame_index += 1
                if frame_index % 250 == 0:
                    save(report, args.output)
                    print(json.dumps({"video": vi+1, "decoded": frame_index,
                                      "observations": len(report["observations"])}), flush=True)
        finally:
            capture.release()
        if frame_index != meta["frames"] or seen != len(samples):
            raise RuntimeError("Incomplete decoding; partial report retained")
        report["videos"][-1]["decode_complete"] = True
        save(report, args.output)
    report["complete"] = True
    save(report, args.output)
    print(json.dumps(report["summary"], ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-report", type=Path)
    parser.add_argument("--gallery", type=Path)
    parser.add_argument("--input", type=Path, default=ROOT / "video")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    parser.add_argument("--prepare-media", action="store_true")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--attach-media", action="store_true")
    parser.add_argument("--preview-dir", type=Path)
    args = parser.parse_args()
    if args.prepare_media and args.attach_media:
        parser.error("Select one media operation")
    if args.attach_media:
        if not args.preview_dir:
            parser.error("--preview-dir is required")
        attach_media(args)
    elif args.prepare_media:
        if not args.source_report:
            parser.error("--source-report is required")
        prepare_media(args)
    else:
        if not args.source_report or not args.gallery:
            parser.error("--source-report and --gallery are required")
        run(args)


if __name__ == "__main__":
    main()
