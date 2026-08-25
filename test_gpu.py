import sys

import torch


cuda_available = torch.cuda.is_available()
print(f"CUDA доступна: {cuda_available}")

if not cuda_available:
    sys.exit(1)

device_name = torch.cuda.get_device_name(0)
capability = torch.cuda.get_device_capability(0)
required_architecture = f"sm_{capability[0]}{capability[1]}"
supported_architectures = torch.cuda.get_arch_list()

print(f"Устройство: {device_name}")
print(f"Compute capability: {capability[0]}.{capability[1]}")
print(f"PyTorch поддерживает: {', '.join(supported_architectures)}")

if required_architecture not in supported_architectures:
    print(f"ОШИБКА: текущий PyTorch не поддерживает {required_architecture}.")
    sys.exit(2)

print("GPU совместим с установленным PyTorch.")
