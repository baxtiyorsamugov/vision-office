import argparse
import sys

import cv2
import insightface
import numpy
import onnxruntime
import pandas
import sqlalchemy
import streamlit
import torch
import ultralytics


parser = argparse.ArgumentParser()
parser.add_argument("--profile", choices=("cpu", "modern", "pascal"), required=True)
args = parser.parse_args()

providers = onnxruntime.get_available_providers()
print(f"Python: {sys.version.split()[0]}")
print(f"PyTorch: {torch.__version__}")
print(f"ONNX Runtime: {onnxruntime.__version__}")
print(f"ONNX providers: {providers}")
print(f"OpenCV: {cv2.__version__}")
print(f"Streamlit: {streamlit.__version__}")
print(f"Profile: {args.profile}")

if args.profile == "cpu":
    print("Installation verified: CPU runtime is ready.")
elif not torch.cuda.is_available() or "CUDAExecutionProvider" not in providers:
    raise SystemExit("GPU runtime verification failed.")
else:
    print("Installation verified: GPU runtime is ready.")
