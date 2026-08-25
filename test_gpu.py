import torch
print(f"CUDA доступна: {torch.cuda.is_available()}")
print(f"Устройство: {torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'Нет'}")