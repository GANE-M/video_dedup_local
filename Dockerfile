FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        libgomp1 \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-docker.txt /app/
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements-docker.txt

# OCR 依赖比较重，默认不装。需要硬字幕自动定位时构建：
# docker build --build-arg INSTALL_OCR=1 -t video-dedup-local:ocr .
ARG INSTALL_OCR=0
COPY requirements-docker-ocr.txt /app/
RUN if [ "$INSTALL_OCR" = "1" ]; then python -m pip install -r requirements-docker-ocr.txt; fi

COPY video_dedup.py subtitle_tool.py batch_pipeline.py config.example.json /app/

VOLUME ["/work"]

ENTRYPOINT ["python", "/app/subtitle_tool.py"]
CMD ["--help"]
