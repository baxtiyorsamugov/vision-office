import ctypes
import os
from pathlib import Path

import insightface
import numpy as np
import onnxruntime as ort

from core.config import ConfigurationError


_DLL_DIRECTORY_HANDLES = []
PROJECT_ROOT = Path(__file__).resolve().parents[2]
INSIGHTFACE_MODEL_ROOT = PROJECT_ROOT / "models" / "insightface"
FACEID_MODEL_PACK = "buffalo_l"


def ensure_faceid_models_available() -> None:
    """Report the missing FaceID pack instead of an InsightFace download traceback."""
    pack_directory = INSIGHTFACE_MODEL_ROOT / "models" / FACEID_MODEL_PACK
    if any(pack_directory.glob("*.onnx")):
        return
    raise ConfigurationError(
        f"FaceID model pack '{FACEID_MODEL_PACK}' was not found in {pack_directory}. "
        "Unpack the approved pack there; the Docker services mount ./models read-only, "
        "so InsightFace cannot download it on the device."
    )


class FaceRecognizer:
    def __init__(self, det_size=(320, 320)):
        ensure_faceid_models_available()
        self.using_cuda = self._cuda_runtime_ready()
        self.app = self._create_app(det_size, self.using_cuda)

        if self.using_cuda and not self._sessions_use_cuda():
            print("[FaceID] CUDA provider is unavailable. Using CPU recognition.")
            self.using_cuda = False
            self.app = self._create_app(det_size, use_cuda=False)

        print(f"[FaceID] Provider: {'CUDA' if self.using_cuda else 'CPU'}")

    @staticmethod
    def _cuda_runtime_ready():
        if "CUDAExecutionProvider" not in ort.get_available_providers():
            return False

        try:
            ort.preload_dlls()
        except AttributeError:
            try:
                import torch

                torch_lib_dir = Path(torch.__file__).parent / "lib"
                if os.name == "nt" and torch_lib_dir.is_dir():
                    _DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(str(torch_lib_dir)))
            except (ImportError, OSError):
                return False
        except OSError:
            return False

        if os.name != "nt":
            return True

        runtime_sets = (
            ("cudnn64_9.dll", "cublas64_12.dll", "cublasLt64_12.dll"),
            ("cudnn64_8.dll", "cublas64_11.dll", "cublasLt64_11.dll"),
        )
        for required_dlls in runtime_sets:
            try:
                for dll_name in required_dlls:
                    ctypes.WinDLL(dll_name)
                return True
            except OSError:
                continue

        print("[FaceID] CUDA/cuDNN runtime is missing. Using CPU recognition.")
        return False

    @staticmethod
    def _create_app(det_size, use_cuda):
        providers = ["CUDAExecutionProvider", "CPUExecutionProvider"] if use_cuda else ["CPUExecutionProvider"]
        app = insightface.app.FaceAnalysis(
            name="buffalo_l",
            root=str(INSIGHTFACE_MODEL_ROOT),
            allowed_modules=["detection", "recognition"],
            providers=providers,
        )
        app.prepare(ctx_id=0 if use_cuda else -1, det_size=det_size)
        return app

    def _sessions_use_cuda(self):
        return all(
            "CUDAExecutionProvider" in model.session.get_providers()
            for model in self.app.models.values()
        )

    def get_embedding(self, face_img):
        faces = self.app.get(face_img)
        if len(faces) > 0:
            return faces[0].embedding
        return None

    def compare(self, emb1, emb2):
        return np.dot(emb1, emb2) / (np.linalg.norm(emb1) * np.linalg.norm(emb2))
