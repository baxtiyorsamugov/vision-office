import cv2
import time
from core.video.streamer import VideoStream
from core.ai.engine import AI_Engine
from core.hr import HRManager
from core.edge.config import load_edge_settings
from core.performance import write_runtime_status
import onnxruntime as ort

def run_test():
    video_path = "test.mp4" 
    print(f"🎬 Запуск быстрого теста: {video_path}")
    
    ai = AI_Engine(detection_imgsz=960)
    hr = None if load_edge_settings().enabled else HRManager(cooldown_minutes=1)
    stream = VideoStream(video_path).start()
    last_status_update = 0.0

    try:
        while True:
            frame = stream.read()
            if frame is None:
                time.sleep(0.005)
                continue

            # Keep original video resolution so the benchmark matches the live pipeline.
            results = ai.process_frame(frame)

            if time.monotonic() - last_status_update >= 1:
                write_runtime_status({
                    "running": True,
                    "onnx_providers": ort.get_available_providers(),
                    **stream.stats(),
                    **ai.status_snapshot(),
                })
                last_status_update = time.monotonic()

            for item in results:
                x1, y1, x2, y2 = item['box']
                name = item['name']
                if hr is not None:
                    hr.register_presence(name)

                cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
                (text_w, text_h), _ = cv2.getTextSize(name, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 2)
                cv2.rectangle(frame, (x1, y1 - text_h - 15), (x1 + text_w + 10, y1), (0, 255, 0), -1)
                cv2.putText(frame, name, (x1 + 5, y1 - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 2)

            preview = cv2.resize(frame, (1280, 720), interpolation=cv2.INTER_AREA)
            cv2.imshow("Smart Office AI - FAST MODE", preview)
            if cv2.waitKey(1) & 0xFF == ord('q'):
                break
    finally:
        write_runtime_status({"running": False, "yolo_device": "stopped"})
        stream.stop()
        ai.stop()
        cv2.destroyAllWindows()

if __name__ == "__main__":
    run_test()
