FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # Keep the model download inside the image rather than a writable volume.
    HF_HOME=/app/.cache/hf

WORKDIR /app

# ffmpeg converts browser recordings to MP3. MediaRecorder writes a streaming
# webm with no duration and no seek index, so those files play but cannot be
# scrubbed, skipped or rewound — in our player or in anything the operator
# downloads them into. MP3 is seekable everywhere, including Windows Media
# Player and iPhones, which webm/opus is not.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Build the index AND download the embedding model at image build time. Without
# this the first caller after a cold start waits ~2s mid-conversation while ONNX
# loads, on top of a model download that can exceed the health-check timeout.
RUN python -c "import app; app.kb.build()" || echo "index warm-up skipped"

EXPOSE 8000

# Render supplies $PORT at runtime; default keeps local `docker run` working.
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
