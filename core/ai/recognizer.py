import insightface
import numpy as np
import onnxruntime as ort # Добавили импорт

class FaceRecognizer:
    def __init__(self, det_size=(320, 320)):
        providers = ort.get_available_providers()
        self.using_cuda = "CUDAExecutionProvider" in providers
        if self.using_cuda:
            try:
                # ONNX Runtime finds CUDA/cuDNN DLLs supplied by the PyTorch wheel.
                ort.preload_dlls()
            except (AttributeError, OSError):
                pass
        active_providers = ["CUDAExecutionProvider", "CPUExecutionProvider"] if self.using_cuda else ["CPUExecutionProvider"]

        try:
            self.app = insightface.app.FaceAnalysis(
                name="buffalo_l",
                allowed_modules=["detection", "recognition"],
                providers=active_providers,
            )
            self.app.prepare(ctx_id=0 if self.using_cuda else -1, det_size=det_size)
        except Exception:
            if not self.using_cuda:
                raise
            self.using_cuda = False
            self.app = insightface.app.FaceAnalysis(
                name="buffalo_l",
                allowed_modules=["detection", "recognition"],
                providers=["CPUExecutionProvider"],
            )
            self.app.prepare(ctx_id=-1, det_size=det_size)

    def get_embedding(self, face_img):
        faces = self.app.get(face_img)
        if len(faces) > 0:
            return faces[0].embedding
        return None

    def compare(self, emb1, emb2):
        return np.dot(emb1, emb2) / (np.linalg.norm(emb1) * np.linalg.norm(emb2))
