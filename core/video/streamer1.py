import cv2
import threading
import time

class VideoStream:
    def __init__(self, src):
        self.stream = cv2.VideoCapture(src)
        # Принудительно ограничиваем буфер (работает на некоторых камерах)
        self.stream.set(cv2.CAP_PROP_BUFFERSIZE, 1) 
        
        self.grabbed, self.frame = self.stream.read()
        self.stopped = False

    def start(self):
        threading.Thread(target=self.update, daemon=True).start()
        return self

    def update(self):
            # Агрессивная очистка очереди (Real-Time Buffer)
            while not self.stopped:
                # grab() работает быстрее read(), он просто захватывает кадр без декодирования
                if self.stream.grab():
                    # Декодируем только самый последний кадр
                    ret, frame = self.stream.retrieve()
                    if ret:
                        self.frame = frame

    def read(self):
        return self.frame

    def stop(self):
        self.stopped = True
        self.stream.release()