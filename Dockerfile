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
RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install --index-url https://download.pytorch.org/whl/cpu \
        torch==2.5.1+cpu torchvision==0.20.1+cpu \
    && python -m pip install -r requirements-docker-cpu.txt

COPY . ./
RUN mkdir -p /app/data /app/models /app/config

CMD ["python", "main.py"]
