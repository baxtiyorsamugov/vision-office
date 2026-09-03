import numpy as np
import multiprocessing as mp
import queue # Импортируем для обработки переполнения
import cv2
import time
import threading
import torch
from collections import deque
from ultralytics import YOLO

ANALYZING_STATUS = "Анализ..."
SEARCHING_STATUS = "Поиск лица..."
UNKNOWN_STATUS = "Неизвестный"
RETRY_INTERVAL_SEC = 0.3
UNKNOWN_RETRY_INTERVAL_SEC = 1.5
EMPLOYEE_CACHE_TTL_SEC = 5
# RTX 3070 comfortably sustains this rate at the configured image size.  Keeping
# this close to the live 25 FPS stream makes an overlay feel attached to a person.
DEFAULT_DETECTION_FPS = 20
MAX_FACES_PER_FRAME = 3
MIN_FACE_SIZE_PX = 48
MIN_TRACK_OBSERVATIONS = 2
STABILIZING_STATUS = "Стабилизация..."


def edge_delivery_worker(stop_event):
    """Network sync/outbox process. It must never delay FaceID inference."""
    from core.edge.config import load_edge_settings
    from core.edge.service import EdgeService

    settings = load_edge_settings()
    if not settings.configured:
        return
    service = EdgeService(settings)
    while not stop_event.is_set():
        try:
            service.maintenance()
        except Exception as error:
            print(f"[Edge] Maintenance error: {error}")
        stop_event.wait(1)

def _load_known_faces(Session, edge_service=None, remote_mode=False):
    if edge_service is not None:
        return edge_service.cache_embeddings()
    if remote_mode:
        return [], None

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
                    names.append({"name": emp.full_name, "person_id": None, "person_type": "employee"})
                    vectors.append(stored_emb / norm)

        if not vectors:
            return [], None

        return names, np.vstack(vectors)
    finally:
        session.close()

def face_recognition_worker(input_queue, shared_memory, face_timings, face_metrics):
    from core.ai.recognizer import FaceRecognizer
    from core.edge.config import load_edge_settings
    from core.edge.service import EdgeService
    from database.manager import get_engine
    from sqlalchemy.orm import sessionmaker
    
    print("🤖 [Worker] Инициализация FaceID...")
    recognizer = FaceRecognizer()
    edge_settings = load_edge_settings()
    edge_service = EdgeService(edge_settings) if edge_settings.configured else None
    engine = get_engine()
    Session = sessionmaker(bind=engine)
    known_names, known_embeddings = _load_known_faces(Session, edge_service, edge_settings.enabled)
    last_cache_refresh = time.monotonic()
    print(f"🤖 [Worker] Загружено эмбеддингов: {len(known_names)}")
    
    print("🤖 [Worker] Разогрев нейросети...")
    dummy_img = np.zeros((112, 112, 3), dtype=np.uint8)
    recognizer.get_embedding(dummy_img)
    print("🤖 [Worker] ГОТОВ К БОЮ!")

    while True:
        try:
            try:
                task = input_queue.get(timeout=1)
            except queue.Empty:
                now = time.monotonic()
                if now - last_cache_refresh > EMPLOYEE_CACHE_TTL_SEC:
                    known_names, known_embeddings = _load_known_faces(Session, edge_service, edge_settings.enabled)
                    last_cache_refresh = now
                continue
            if task is None: break
                
            track_id, face_img = task

            now = time.monotonic()
            if now - last_cache_refresh > EMPLOYEE_CACHE_TTL_SEC:
                known_names, known_embeddings = _load_known_faces(Session, edge_service, edge_settings.enabled)
                last_cache_refresh = now
            
            face_started = time.perf_counter()
            embedding = recognizer.get_embedding(face_img)
            face_ms = (time.perf_counter() - face_started) * 1000
            face_timings.append(face_ms)
            if len(face_timings) > 100:
                del face_timings[0]
            face_metrics["last_ms"] = face_ms
            face_metrics["count"] = int(face_metrics.get("count", 0)) + 1
            if embedding is not None and known_embeddings is not None:
                embedding = np.asarray(embedding, dtype=np.float32)
                norm = np.linalg.norm(embedding)
                if norm == 0:
                    shared_memory[track_id] = {"name": SEARCHING_STATUS}
                    continue

                sims = known_embeddings @ (embedding / norm)
                best_idx = int(np.argmax(sims))
                max_sim = float(sims[best_idx])
                best_match = known_names[best_idx]
                
                threshold = edge_settings.recognition_threshold if edge_service else 0.25
                if max_sim > threshold:
                    shared_memory[track_id] = {"name": best_match["name"], **best_match, "confidence": max_sim}
                    if edge_service:
                        edge_service.queue_recognition(best_match, max_sim, embedding, subject_hint=f"track:{track_id}")
                    print(f"✅ Узнал: {best_match['name']} ({max_sim:.2f})")
                else:
                    shared_memory[track_id] = {"name": UNKNOWN_STATUS, "confidence": max_sim}
                    if edge_service:
                        edge_service.queue_recognition(None, max_sim, embedding, subject_hint=f"unknown:{track_id}")
            elif embedding is not None and edge_service:
                # A synchronized but empty cache still means this is an unknown face.
                embedding = np.asarray(embedding, dtype=np.float32)
                shared_memory[track_id] = {"name": UNKNOWN_STATUS}
                edge_service.queue_recognition(None, None, embedding, subject_hint=f"unknown:{track_id}")
            else:
                # Если лицо отвернуто/размыто, пишем статус, чтобы попробовать еще раз
                shared_memory[track_id] = {"name": SEARCHING_STATUS}
                
        except Exception as e:
            print(f"❌ Ошибка Worker: {e}")

class AI_Engine:
    def __init__(self, detection_imgsz=960, detection_fps=DEFAULT_DETECTION_FPS):
        self.model = YOLO("yolov8n-face.pt")
        self.detection_imgsz = max(640, int(detection_imgsz))
        self.detection_interval_sec = 1 / max(1, min(30, float(detection_fps)))
        self.using_cuda = torch.cuda.is_available()
        if self.using_cuda:
            torch.backends.cudnn.benchmark = True
        self.inference_options = {
            "device": 0 if self.using_cuda else "cpu",
            "imgsz": self.detection_imgsz,
        }
        print(f"[AI] YOLO device: {'CUDA:0 (FP32)' if self.using_cuda else 'CPU'}")
        self.manager = mp.Manager()
        self.shared_memory = self.manager.dict()
        self.last_retry_at = {}
        self.track_observations = {}
        self.last_detection_at = 0.0
        self.last_processed_data = []
        self.results_lock = threading.Lock()
        self.inference_lock = threading.Lock()
        self.inference_in_progress = False
        self.pending_frame = None
        self.detection_condition = threading.Condition()
        self.detection_stop_requested = False
        self.detection_timings = deque(maxlen=100)
        self.detection_started_at = deque(maxlen=100)
        self.last_metrics_write_at = 0.0
        
        # 🔥 СУПЕР-ФИКС: Очередь размером в 2 кадра. Никаких пробок!
        self.input_queue = mp.Queue(maxsize=2)
        self.face_timings = self.manager.list()
        self.face_metrics = self.manager.dict({"count": 0, "last_ms": None, "dropped": 0})
        self.edge_stop_event = mp.Event()
        self.edge_worker = mp.Process(target=edge_delivery_worker, args=(self.edge_stop_event,), daemon=True)
        self.edge_worker.start()

        # One long-lived CUDA thread is essential. Creating a thread per frame makes
        # CUDA initialize a new context every time, which stalls detection for seconds.
        self.detector_thread = threading.Thread(target=self._detection_worker, daemon=True)
        self.detector_thread.start()
        
        self.worker = mp.Process(target=face_recognition_worker, args=(self.input_queue, self.shared_memory, self.face_timings, self.face_metrics), daemon=True)
        self.worker.start()

    def _warm_up_model(self):
        """Pay model/tracker initialization cost before opening the live camera window."""
        print("[AI] Warming up detector...")
        warmup_frame = np.zeros((self.detection_imgsz, self.detection_imgsz, 3), dtype=np.uint8)
        try:
            self.model.track(warmup_frame, persist=True, tracker="bytetrack.yaml", verbose=False, **self.inference_options)
        except Exception as error:
            print(f"[AI] Detector warm-up failed: {error}")
        else:
            print("[AI] Detector ready.")

    def _detection_worker(self):
        """Run all YOLO work in one CUDA-owning thread and keep only the newest frame."""
        self._warm_up_model()
        while True:
            with self.detection_condition:
                while self.pending_frame is None and not self.detection_stop_requested:
                    self.detection_condition.wait(timeout=1)
                if self.detection_stop_requested:
                    return
                frame = self.pending_frame
                self.pending_frame = None

            with self.inference_lock:
                self.inference_in_progress = True
            self._run_detection(frame)

    def process_frame(self, frame):
        now = time.monotonic()
        if now - self.last_detection_at >= self.detection_interval_sec:
            self.last_detection_at = now
            # A one-slot mailbox: when YOLO is busy, replace stale video with the latest frame.
            with self.detection_condition:
                self.pending_frame = frame.copy()
                self.detection_condition.notify()

        with self.results_lock:
            output = []
            for item in self.last_processed_data:
                result = self._result_for(item["id"], item["name"])
                output.append({
                    "id": item["id"], "name": result["name"],
                    "person_id": result.get("person_id"), "person_type": result.get("person_type"),
                    "confidence": result.get("confidence"), "box": item["box"],
                })
            return output

    def _result_for(self, track_id, fallback_name):
        result = self.shared_memory.get(track_id)
        if isinstance(result, dict):
            return result
        # Compatibility with an already-running worker from an earlier version.
        return {"name": result or fallback_name}

    def _run_detection(self, frame):
        try:
            started = time.perf_counter()
            results = self.model.track(
                frame,
                persist=True,
                tracker="bytetrack.yaml",
                verbose=False,
                **self.inference_options,
            )
            elapsed_ms = (time.perf_counter() - started) * 1000
            self.detection_timings.append(elapsed_ms)
            self.detection_started_at.append(time.monotonic())
            processed_data = []

            if results[0].boxes.id is not None:
                boxes = results[0].boxes.xyxy.cpu().numpy().astype(int)
                ids = results[0].boxes.id.cpu().numpy().astype(int)

                candidates = sorted(zip(boxes, ids), key=lambda item: (item[0][2] - item[0][0]) * (item[0][3] - item[0][1]), reverse=True)
                for box, track_id in candidates[:MAX_FACES_PER_FRAME]:
                    track_id = int(track_id)
                    self.track_observations[track_id] = self.track_observations.get(track_id, 0) + 1
                    current_result = self._result_for(track_id, None)
                    current_status = current_result.get("name")

                    if current_status is not None and current_status not in [SEARCHING_STATUS, ANALYZING_STATUS, STABILIZING_STATUS, UNKNOWN_STATUS]:
                        name = current_status
                    elif current_status == ANALYZING_STATUS:
                        name = ANALYZING_STATUS
                    else:
                        now = time.monotonic()
                        retry_interval = UNKNOWN_RETRY_INTERVAL_SEC if current_status == UNKNOWN_STATUS else RETRY_INTERVAL_SEC
                        if current_status in [SEARCHING_STATUS, UNKNOWN_STATUS] and now - self.last_retry_at.get(track_id, 0) < retry_interval:
                            processed_data.append({"id": track_id, "name": current_status, "box": box})
                            continue

                        box_w, box_h = box[2] - box[0], box[3] - box[1]
                        if min(box_w, box_h) < MIN_FACE_SIZE_PX or self.track_observations[track_id] < MIN_TRACK_OBSERVATIONS:
                            self.shared_memory[track_id] = {"name": STABILIZING_STATUS}
                            processed_data.append({"id": track_id, "name": STABILIZING_STATUS, "box": box})
                            continue

                        self.shared_memory[track_id] = {"name": ANALYZING_STATUS}
                        self.last_retry_at[track_id] = now
                        name = ANALYZING_STATUS

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
                                self.shared_memory[track_id] = {"name": SEARCHING_STATUS}
                                self.face_metrics["dropped"] = int(self.face_metrics.get("dropped", 0)) + 1

                    processed_data.append({"id": track_id, "name": name, "box": box})

            with self.results_lock:
                self.last_processed_data = processed_data
        except Exception as error:
            print(f"[AI] Detection error: {error}")
        finally:
            with self.inference_lock:
                self.inference_in_progress = False

    def status_snapshot(self):
        detection = list(self.detection_timings)
        face = list(self.face_timings)
        def percentile(values, value):
            if not values:
                return None
            return round(float(np.percentile(values, value)), 1)
        now = time.monotonic()
        recent_detections = [item for item in self.detection_started_at if now - item <= 1]
        try:
            queue_size = self.input_queue.qsize()
        except (NotImplementedError, OSError):
            queue_size = None
        return {
            "yolo_device": "CUDA:0 (FP32)" if self.using_cuda else "CPU",
            "detection_fps": round(float(len(recent_detections)), 1),
            "detection_ms_p50": percentile(detection, 50),
            "detection_ms_p95": percentile(detection, 95),
            "face_ms_p50": percentile(face, 50),
            "face_ms_p95": percentile(face, 95),
            "face_tasks": int(self.face_metrics.get("count", 0)),
            "face_dropped": int(self.face_metrics.get("dropped", 0)),
            "face_queue_size": queue_size,
        }

    def stop(self):
        with self.detection_condition:
            self.detection_stop_requested = True
            self.detection_condition.notify_all()
        self.detector_thread.join(timeout=3)

        try:
            self.input_queue.put(None, timeout=1)
        except queue.Full:
            pass

        self.worker.join(timeout=2)
        if self.worker.is_alive():
            self.worker.terminate()
            self.worker.join(timeout=1)
        self.manager.shutdown()
        self.edge_stop_event.set()
        self.edge_worker.join(timeout=2)
        if self.edge_worker.is_alive():
            self.edge_worker.terminate()
