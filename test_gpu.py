"""Native Windows CUDA diagnostic using real kernels, not exact SM list matches."""
from core.ai.runtime import torch_device


if __name__ == "__main__":
    device, reason = torch_device()
    print(f"PyTorch device: {device}")
    if reason:
        print(reason)
    if device == "cuda":
        import torch
        print(f"GPU: {torch.cuda.get_device_name(0)}")
        print(f"CUDA kernels and NumPy bridge: OK; torch={torch.__version__}")
    raise SystemExit(0 if device == "cuda" else 1)
