FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=UTC \
    PORNSCAN_HOST=0.0.0.0

WORKDIR /app

# PORNSCAN_MODEL=yolo needs the (large) ultralytics/torch stack:
#   docker compose build --build-arg WITH_YOLO=1 pornscan
ARG WITH_YOLO=0

# opencv/nudenet runtime shared objects
RUN apt-get update && apt-get install -y --no-install-recommends \
        libglib2.0-0 libxcb1 libgl1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY requirements-yolo.txt .
RUN if [ "$WITH_YOLO" = "1" ]; then pip install --no-cache-dir -r requirements-yolo.txt; fi

COPY main.py .

RUN mkdir -p /root/.cache

EXPOSE 8010

CMD ["python", "main.py"]
