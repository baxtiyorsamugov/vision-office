# core/manager.py
from core.video.streamer import VideoStream
from core.ai.engine import AI_Engine
import threading

class OfficeManager:
    def __init__(self):
        self.detector = AI_Engine() # Наш ИИ движок
        self.cameras = {} 
        self.stats = {"people_count": 0}

    def add_camera(self, cam_id, rtsp_url):
        self.cameras[cam_id] = VideoStream(rtsp_url).start()

    def get_frame(self, cam_id):
        return self.cameras[cam_id].read() if cam_id in self.cameras else None

# Создаем глобальный объект
manager = OfficeManager()