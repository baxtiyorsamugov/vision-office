import cv2
import time
from core.video.streamer import VideoStream
from core.ai.engine import AI_Engine
from core.hr import HRManager

def run_test():
    video_path = "test.mp4" 
    print(f"🎬 Запуск быстрого теста: {video_path}")
    
    ai = AI_Engine()
    hr = HRManager(cooldown_minutes=1)
    stream = VideoStream(video_path).start()

    try:
        while True:
            frame = stream.read()
            if frame is None:
                time.sleep(0.005)
                continue

            frame = cv2.resize(frame, (1280, 720))
            results = ai.process_frame(frame)

            for item in results:
                x1, y1, x2, y2 = item['box']
                name = item['name']
                hr.register_presence(name)

                cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
                (text_w, text_h), _ = cv2.getTextSize(name, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 2)
                cv2.rectangle(frame, (x1, y1 - text_h - 15), (x1 + text_w + 10, y1), (0, 255, 0), -1)
                cv2.putText(frame, name, (x1 + 5, y1 - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 2)

            cv2.imshow("Smart Office AI - FAST MODE", frame)
            if cv2.waitKey(1) & 0xFF == ord('q'):
                break
    finally:
        stream.stop()
        ai.stop()
        cv2.destroyAllWindows()

if __name__ == "__main__":
    run_test()
