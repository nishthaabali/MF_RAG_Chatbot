# API image for Railway (docs/deployment-plan.md §4.5). The frontend is served by Vercel, but it is
# copied too so `docker run` locally shows the UI on the same origin.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONUTF8=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/opt/hf
WORKDIR /app
COPY pyproject.toml ./
COPY backend ./backend
COPY config ./config
COPY frontend ./frontend
# CPU-only torch first (the default wheel adds gigabytes of CUDA libraries), then the project as
# an editable install: app/config.py derives ROOT_DIR (= /app) from the source layout.
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install -e .
# Bake the embedding model into the image: no download at startup, no Hugging Face dependency.
RUN python -c "from sentence_transformers import SentenceTransformer as S; S('BAAI/bge-small-en-v1.5')"
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
# Railway mounts the volume here; the folder also exists for a plain `docker run`.
RUN mkdir -p /app/data
WORKDIR /app/backend
EXPOSE 8000
# Install the first data bundle on an empty volume (always exits 0), then serve.
CMD ["sh", "-c", "python -m app.bootstrap && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
