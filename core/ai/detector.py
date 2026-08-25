from ultralytics import YOLO

class FaceDetector:
    def __init__(self, model_path="yolov8n-face.pt"): # Возвращаем .pt
        self.model = YOLO(model_path) # Здесь уже не нужны лишние параметры

    def detect(self, frame):
        results = self.model.predict(frame, imgsz=640, conf=0.5, verbose=False)
        
        detections = []
        for r in results:
            boxes = r.boxes
            for box in boxes:
                x1, y1, x2, y2 = box.xyxy[0].tolist()
                conf = box.conf[0].item()
                detections.append({'box': [int(x1), int(y1), int(x2), int(y2)], 'conf': conf})
        return detections