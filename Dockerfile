# Stable CPU image for the Vision Office Edge services.
FROM python:3.10-slim-bookworm AS dependencies

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# ffmpeg and OpenCV runtime libraries are required for RTSP/H.264 decoding.
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        build-essential \
        ffmpeg \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-docker-cpu.txt ./

# CPU wheels keep support services and the default deployment portable.
# The optional GPU target replaces only the inference runtime packages.
# The PyTorch CPU index publishes the "+cpu" local version only for x86_64;
# its aarch64 wheels for the same release carry no suffix. Both are CPU builds.
RUN python -m pip install --upgrade pip setuptools wheel \
    && if [ "$(uname -m)" = "aarch64" ]; then \
        TORCH="torch==2.5.1 torchvision==0.20.1"; \
    else \
        TORCH="torch==2.5.1+cpu torchvision==0.20.1+cpu"; \
    fi \
    && python -m pip install --index-url https://download.pytorch.org/whl/cpu $TORCH \
    && python -m pip install -r requirements-docker-cpu.txt

FROM dependencies AS cpu
COPY . ./
RUN mkdir -p /app/data /app/models /app/config \
    && sed -i 's/\r$//' docker/entrypoint.sh \
    && chmod +x docker/entrypoint.sh

ENTRYPOINT ["./docker/entrypoint.sh"]
CMD ["python", "main.py"]

# CUDA libraries are supplied by official PyTorch wheels, not host CUDA installs.
FROM dependencies AS gpu-dependencies
ARG GPU_PROFILE=cu124
RUN python -m pip uninstall -y torch torchvision onnxruntime \
    && case "$GPU_PROFILE" in \
         cu124) TORCH=2.5.1+cu124; VISION=0.20.1+cu124 ;; \
         cu128) TORCH=2.7.1+cu128; VISION=0.22.1+cu128 ;; \
         *) echo "Unsupported GPU_PROFILE"; exit 2 ;; \
       esac \
    && python -m pip install --index-url https://download.pytorch.org/whl/$GPU_PROFILE \
         "torch==$TORCH" "torchvision==$VISION" \
    && python -m pip install onnxruntime-gpu==1.23.2 \
    && python -m pip check

FROM gpu-dependencies AS gpu
COPY . ./
RUN mkdir -p /app/data /app/models /app/config \
    && sed -i 's/\r$//' docker/entrypoint.sh \
    && chmod +x docker/entrypoint.sh
ENTRYPOINT ["./docker/entrypoint.sh"]
CMD ["python", "main.py"]

# Plain docker compose remains CPU-only on any machine.
FROM cpu AS final
