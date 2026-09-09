import ctypes
import os
from pathlib import Path

import insightface
import numpy as np
import onnxruntime as ort
from core.ai.runtime import cpu_threads, wants_cuda

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
        self.det_size = det_size
        self.fallback_reason = None
        self.using_cuda = self._cuda_runtime_ready()
        if not self.using_cuda:
            self.fallback_reason = "CUDA runtime unavailable to FaceID" if wants_cuda() else "CPU explicitly selected"
        try:
            self.app = self._create_app(det_size, self.using_cuda)
            if self.using_cuda:
                if not self._sessions_use_cuda():
                    raise RuntimeError("CUDA provider not active in all FaceID sessions")
                self.sanity_check()
        except Exception as error:
            if not self.using_cuda:
                raise
            self._fallback_to_cpu(error)

        print(f"[FaceID] Provider: {'CUDA' if self.using_cuda else 'CPU'}")

    @staticmethod
    def _cuda_runtime_ready():
        if not wants_cuda():
            return False
        if "CUDAExecutionProvider" not in ort.get_available_providers():
            return False

        try:
            # Linux wheel libraries are loaded by torch before ORT opens cuDNN.
            import torch
            ort.preload_dlls()
        except AttributeError:
            try:
                import torch

                torch_lib_dir = Path(torch.__file__).parent / "lib"
                if os.name == "nt" and torch_lib_dir.is_dir():
                    _DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(str(torch_lib_dir)))
            except (ImportError, OSError):
                return False
        except (ImportError, OSError, RuntimeError):
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
        providers = [("CUDAExecutionProvider", {
            "device_id": 0,
            "gpu_mem_limit": 512 * 1024 * 1024,
            "arena_extend_strategy": "kSameAsRequested",
            "cudnn_conv_algo_search": "HEURISTIC",
            "cudnn_conv_use_max_workspace": "0",
        }), "CPUExecutionProvider"] if use_cuda else ["CPUExecutionProvider"]
        options = ort.SessionOptions()
        options.intra_op_num_threads = cpu_threads()
        options.inter_op_num_threads = 1
        app = insightface.app.FaceAnalysis(
            name="buffalo_l",
            root=str(INSIGHTFACE_MODEL_ROOT),
            allowed_modules=["detection", "recognition"],
            providers=providers,
            sess_options=options,
        )
        app.prepare(ctx_id=0 if use_cuda else -1, det_size=det_size)
        return app

    def _sessions_use_cuda(self):
        return all(
            "CUDAExecutionProvider" in model.session.get_providers()
            for model in self.app.models.values()
        )

    def get_embedding(self, face_img):
        try:
            faces = self.app.get(face_img)
        except Exception as error:
            if not self.using_cuda:
                raise
            self._fallback_to_cpu(error)
            faces = self.app.get(face_img)
        if len(faces) > 0:
            return faces[0].embedding
        return None

    def _fallback_to_cpu(self, error):
        self.fallback_reason = f"CUDA FaceID failed: {type(error).__name__}"
        print(f"[FaceID] {self.fallback_reason}; switching to CPU.")
        self.using_cuda = False
        self.app = None
        self.app = self._create_app(self.det_size, use_cuda=False)

    def sanity_check(self):
        """Exercise detection AND recognition even when a blank frame has no face."""
        self.app.get(np.zeros((320, 320, 3), dtype=np.uint8))
        vector = self.app.models["recognition"].get_feat(np.zeros((112, 112, 3), dtype=np.uint8))
        if np.asarray(vector).size != 512 or not np.isfinite(vector).all():
            raise RuntimeError("Invalid FaceID output")

    def compare(self, emb1, emb2):
        return np.dot(emb1, emb2) / (np.linalg.norm(emb1) * np.linalg.norm(emb2))
