# Stable CPU image for the Vision Office Edge services.
FROM python:3.10-slim-bookworm

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

# CPU wheels make the default deployment portable. Docker GPU support is an
# optional future profile; the Windows native installer remains available.
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

COPY . ./
RUN mkdir -p /app/data /app/models /app/config \
    && chmod +x docker/entrypoint.sh

ENTRYPOINT ["./docker/entrypoint.sh"]
CMD ["python", "main.py"]
