"""Small process-local runtime decisions shared by inference and diagnostics."""
import os


def cpu_threads():
    return max(1, min(8, int(os.getenv("VISION_OFFICE_CPU_THREADS", "2"))))


def limit_torch_threads():
    import torch
    threads = cpu_threads()
    # Ultralytics CPU device setup overrides the initial PyTorch thread count.
    if torch.get_num_threads() != threads:
        torch.set_num_threads(threads)


def wants_cuda():
    mode = os.getenv("VISION_OFFICE_DEVICE", "auto").lower()
    if mode not in {"auto", "cpu", "cuda"}:
        raise ValueError("VISION_OFFICE_DEVICE must be auto, cpu or cuda")
    return mode != "cpu"


def torch_device():
    import torch
    limit_torch_threads()
    if not wants_cuda():
        return "cpu", "CPU explicitly selected"
    if not torch.cuda.is_available():
        return "cpu", "CUDA unavailable to PyTorch"
    try:
        # Architecture lists alone reject compatible GPUs (e.g. sm_89 on sm_86).
        # Exercise cuDNN, torchvision CUDA NMS and the NumPy bridge instead.
        from torchvision.ops import nms
        with torch.inference_mode():
            x = torch.ones((1, 3, 32, 32), device="cuda")
            w = torch.ones((4, 3, 3, 3), device="cuda")
            y = torch.nn.functional.conv2d(x, w)
            assert bool(torch.isfinite(y).all())
            nms(torch.tensor([[0., 0., 8., 8.]], device="cuda"),
                torch.ones(1, device="cuda"), 0.5)
            assert y.cpu().numpy().size > 0
            torch.cuda.synchronize()
        # Benchmark autotuning can reserve large workspaces on small GPUs.
        torch.backends.cudnn.benchmark = False
        return "cuda", None
    except Exception as error:
        return "cpu", f"CUDA compute test failed: {type(error).__name__}"
