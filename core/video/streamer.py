import cv2
import threading
import time

class VideoStream:
    def __init__(self, src):
        self.src = src
        self.is_file = not (str(src).startswith('rtsp://') or str(src) == '0')
        self.lock = threading.Lock()
        self.frame_index = 0
        self.last_read_index = 0
        self.published_at = 0.0
        self.capture_fps = 0.0
        self._fps_window_started = time.monotonic()
        self._fps_window_frames = 0
        
        self.stream = cv2.VideoCapture(src)
        
        # Если это RTSP, убираем буфер
        if not self.is_file:
            self.stream.set(cv2.CAP_PROP_BUFFERSIZE, 1) 
            
        self.grabbed, self.frame = self.stream.read()
        if self.grabbed:
            self.frame_index = 1
        self.stopped = False

    def start(self):
        threading.Thread(target=self.update, daemon=True).start()
        return self

    def update(self):
        while not self.stopped:
            if self.is_file:
                # Читаем ФАЙЛ (обычный read, чтобы видео не неслось как сумасшедшее)
                self.grabbed, frame = self.stream.read()
                time.sleep(0.03) # Имитация 30 FPS
                
                # Зацикливаем видео, если оно закончилось
                if not self.grabbed:
                    self.stream.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    continue
                self._publish(frame)
            else:
                # Читаем RTSP (Агрессивный сброс буфера)
                if self.stream.grab():
                    ret, frame = self.stream.retrieve()
                    if ret:
                        self._publish(frame)
                    else:
                        time.sleep(0.01)
                else:
                    time.sleep(0.01)

    def _publish(self, frame):
        with self.lock:
            self.frame = frame
            self.frame_index += 1
            self.published_at = time.monotonic()
            self._fps_window_frames += 1
            elapsed = self.published_at - self._fps_window_started
            if elapsed >= 1:
                self.capture_fps = self._fps_window_frames / elapsed
                self._fps_window_frames = 0
                self._fps_window_started = self.published_at

    def read(self):
        with self.lock:
            if self.frame_index == self.last_read_index:
                return None
            self.last_read_index = self.frame_index
            return self.frame

    def stats(self):
        with self.lock:
            return {
                "capture_fps": round(self.capture_fps, 1),
                "frame_age_ms": round(max(0.0, time.monotonic() - self.published_at) * 1000, 1) if self.published_at else None,
            }

    def stop(self):
        self.stopped = True
        self.stream.release()
