import numpy as np
import multiprocessing as mp
import queue # Импортируем для обработки переполнения
import cv2
import time
import threading
import torch
from ultralytics import YOLO

ANALYZING_STATUS = "Анализ..."
SEARCHING_STATUS = "Поиск лица..."
UNKNOWN_STATUS = "Неизвестный"
RETRY_INTERVAL_SEC = 0.3
EMPLOYEE_CACHE_TTL_SEC = 30
DETECTION_INTERVAL_SEC = 1 / 12

def _load_known_faces(Session):
    from database.models import Employee

    session = Session()
    try:
        employees = session.query(Employee).all()
        names = []
        vectors = []

        for emp in employees:
            for stored_v in emp.face_embeddings or []:
                stored_emb = np.asarray(stored_v, dtype=np.float32)
                norm = np.linalg.norm(stored_emb)
                if norm > 0:
                    names.append(emp.full_name)
                    vectors.append(stored_emb / norm)

        if not vectors:
            return [], None

        return names, np.vstack(vectors)
    finally:
        session.close()

def face_recognition_worker(input_queue, shared_memory):
    from core.ai.recognizer import FaceRecognizer
    from database.manager import get_engine
    from sqlalchemy.orm import sessionmaker
    
    print("🤖 [Worker] Инициализация FaceID...")
    recognizer = FaceRecognizer()
    engine = get_engine()
    Session = sessionmaker(bind=engine)
    known_names, known_embeddings = _load_known_faces(Session)
    last_cache_refresh = time.monotonic()
    print(f"🤖 [Worker] Загружено эмбеддингов: {len(known_names)}")
    
    print("🤖 [Worker] Разогрев нейросети...")
    dummy_img = np.zeros((112, 112, 3), dtype=np.uint8)
    recognizer.get_embedding(dummy_img)
    print("🤖 [Worker] ГОТОВ К БОЮ!")

    while True:
        try:
            task = input_queue.get()
            if task is None: break
                
            track_id, face_img = task

            now = time.monotonic()
            if now - last_cache_refresh > EMPLOYEE_CACHE_TTL_SEC:
                known_names, known_embeddings = _load_known_faces(Session)
                last_cache_refresh = now
            
            embedding = recognizer.get_embedding(face_img)
            if embedding is not None and known_embeddings is not None:
                embedding = np.asarray(embedding, dtype=np.float32)
                norm = np.linalg.norm(embedding)
                if norm == 0:
                    shared_memory[track_id] = SEARCHING_STATUS
                    continue

                sims = known_embeddings @ (embedding / norm)
                best_idx = int(np.argmax(sims))
                max_sim = float(sims[best_idx])
                best_match = known_names[best_idx]
                
                if max_sim > 0.25:
                    shared_memory[track_id] = best_match
                    print(f"✅ Узнал: {best_match} ({max_sim:.2f})")
                else:
                    shared_memory[track_id] = UNKNOWN_STATUS
            else:
                # Если лицо отвернуто/размыто, пишем статус, чтобы попробовать еще раз
                shared_memory[track_id] = SEARCHING_STATUS
                
        except Exception as e:
            print(f"❌ Ошибка Worker: {e}")

class AI_Engine:
    def __init__(self):
        self.model = YOLO("yolov8n-face.pt")
        self.using_cuda = torch.cuda.is_available()
        if self.using_cuda:
            torch.backends.cudnn.benchmark = True
        self.inference_options = {
            "device": 0 if self.using_cuda else "cpu",
            "quantize": 16 if self.using_cuda else None,
            "imgsz": 640,
        }
        print(f"[AI] YOLO device: {'CUDA:0 (FP16)' if self.using_cuda else 'CPU'}")
        self.manager = mp.Manager()
        self.shared_memory = self.manager.dict()
        self.last_retry_at = {}
        self.last_detection_at = 0.0
        self.last_processed_data = []
        self.results_lock = threading.Lock()
        self.inference_lock = threading.Lock()
        self.inference_in_progress = False
        self.inference_thread = None
        
        # 🔥 СУПЕР-ФИКС: Очередь размером в 2 кадра. Никаких пробок!
        self.input_queue = mp.Queue(maxsize=2)
        
        self.worker = mp.Process(target=face_recognition_worker, args=(self.input_queue, self.shared_memory), daemon=True)
        self.worker.start()

    def process_frame(self, frame):
        now = time.monotonic()
        if now - self.last_detection_at >= DETECTION_INTERVAL_SEC:
            with self.inference_lock:
                self.inference_in_progress = True
            self.last_detection_at = now
            self._run_detection(frame)

        with self.results_lock:
            return [
                {
                    "id": item["id"],
                    "name": self.shared_memory.get(item["id"], item["name"]),
                    "box": item["box"],
                }
                for item in self.last_processed_data
            ]

    def _run_detection(self, frame):
        try:
            results = self.model.track(
                frame,
                persist=True,
                tracker="bytetrack.yaml",
                verbose=False,
                **self.inference_options,
            )
            processed_data = []

            if results[0].boxes.id is not None:
                boxes = results[0].boxes.xyxy.cpu().numpy().astype(int)
                ids = results[0].boxes.id.cpu().numpy().astype(int)

                for box, track_id in zip(boxes, ids):
                    track_id = int(track_id)
                    current_status = self.shared_memory.get(track_id)

                    if current_status is not None and current_status not in [SEARCHING_STATUS, ANALYZING_STATUS]:
                        name = current_status
                    elif current_status == ANALYZING_STATUS:
                        name = ANALYZING_STATUS
                    else:
                        now = time.monotonic()
                        if current_status == SEARCHING_STATUS and now - self.last_retry_at.get(track_id, 0) < RETRY_INTERVAL_SEC:
                            processed_data.append({"id": track_id, "name": current_status, "box": box})
                            continue

                        self.shared_memory[track_id] = ANALYZING_STATUS
                        self.last_retry_at[track_id] = now
                        name = ANALYZING_STATUS

                        box_w, box_h = box[2] - box[0], box[3] - box[1]
                        pad_x = int(box_w * 0.20)
                        pad_y = int(box_h * 0.35)

                        h, w = frame.shape[:2]
                        x1, y1, x2, y2 = box
                        crop_x1, crop_y1 = max(0, x1 - pad_x), max(0, y1 - pad_y)
                        crop_x2, crop_y2 = min(w, x2 + pad_x), min(h, y2 + pad_y)
                        face_img = frame[crop_y1:crop_y2, crop_x1:crop_x2]

                        if face_img.size > 0:
                            if face_img.shape[0] < 112 or face_img.shape[1] < 112:
                                face_img = cv2.resize(face_img, (150, 150), interpolation=cv2.INTER_CUBIC)

                            try:
                                self.input_queue.put_nowait((track_id, face_img.copy()))
                            except queue.Full:
                                self.shared_memory[track_id] = SEARCHING_STATUS

                    processed_data.append({"id": track_id, "name": name, "box": box})

            with self.results_lock:
                self.last_processed_data = processed_data
        except Exception as error:
            print(f"[AI] Detection error: {error}")
        finally:
            with self.inference_lock:
                self.inference_in_progress = False

    def stop(self):
        if self.inference_thread is not None:
            self.inference_thread.join(timeout=2)

        try:
            self.input_queue.put(None, timeout=1)
        except queue.Full:
            pass

        self.worker.join(timeout=2)
        if self.worker.is_alive():
            self.worker.terminate()
            self.worker.join(timeout=1)
        self.manager.shutdown()
