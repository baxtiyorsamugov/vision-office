import argparse
import cv2
import time
from core.video.streamer import VideoStream
from core.ai.engine import AI_Engine
from core.hr import HRManager
from core.edge.config import load_edge_settings
from core.overlay import draw_detection_label
from core.performance import write_camera_runtime_status
import onnxruntime as ort

def run_test(headless=False, duration_seconds=None):
    video_path = "test.mp4" 
    print(f"Starting test video: {video_path}")
    
    ai = AI_Engine(detection_imgsz=960, camera_id="test_video", event_type="entry")
    hr = HRManager(cooldown_minutes=1)
    stream = VideoStream(video_path).start()
    last_status_update = 0.0
    started_at = time.monotonic()

    try:
        while True:
            if time.monotonic() - last_status_update >= 1:
                write_camera_runtime_status("test_video", {
                    "running": True,
                    "camera_name": "Test video",
                    "event_type": "entry",
                    "onnx_providers": ort.get_available_providers(),
                    **stream.stats(),
                    **ai.status_snapshot(),
                })
                last_status_update = time.monotonic()
            frame = stream.read()
            if frame is None:
                time.sleep(0.005)
                continue

            # Keep original video resolution so the benchmark matches the live pipeline.
            results = ai.process_frame(frame)

            for item in results:
                x1, y1, x2, y2 = item['box']
                name = item['name']
                hr.register_presence(item, event_type="entry")

                frame = draw_detection_label(frame, (x1, y1, x2, y2), name)

            preview = cv2.resize(frame, (1280, 720), interpolation=cv2.INTER_AREA)
            if not headless:
                cv2.imshow("Smart Office AI - FAST MODE", preview)
            if duration_seconds and time.monotonic() - started_at >= duration_seconds:
                break
            if not headless and cv2.waitKey(1) & 0xFF == ord('q'):
                break
    finally:
        write_camera_runtime_status("test_video", {"running": False, "camera_name": "Test video", **stream.stats()})
        stream.stop()
        ai.stop()
        cv2.destroyAllWindows()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--duration-seconds", type=int, default=None)
    args = parser.parse_args()
    run_test(headless=args.headless, duration_seconds=args.duration_seconds)
