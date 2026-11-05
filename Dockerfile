# Backend: FastAPI, the agent pipeline, the dashboard and Arianna.
#
# Python 3.12. The torch pin that used to force this is gone; what holds the
# version now is only that the rest of requirements.txt was installed and
# tested against it — faster-whisper 1.0.3 with its ctranslate2 and
# onnxruntime wheels, and the langgraph pins. Moving to 3.13 means checking
# those, which is its own change rather than a side effect of this one.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=5 \
    HF_HOME=/models

# ffmpeg: faster-whisper decodes voice notes through it. curl: the healthcheck.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY evals/ ./evals/
COPY scripts/hash_password.py ./scripts/hash_password.py

# Nothing here needs root at runtime. data/ and /models are volumes, so create
# them owned by the app user — a fresh volume inherits the mountpoint's owner.
RUN useradd --create-home --uid 10001 app \
 && mkdir -p /app/data /models \
 && chown -R app:app /app /models
USER app

EXPOSE 8000

# start-period is generous: the first request that touches a voice note
# downloads a model, and we would rather not be restarted mid-download.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
