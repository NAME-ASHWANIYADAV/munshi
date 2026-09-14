# MunshiJi API — optional container image.
# The normal development path is the virtualenv (scripts/setup.ps1); this exists so the whole
# stack can come up with `docker compose --profile full up`.

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Kolkata

WORKDIR /app

# Dependencies first so the layer caches across source edits.
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY pyproject.toml ./
COPY munshiji ./munshiji
RUN pip install --no-deps -e .

# The SQLite database and generated audio live here; mounted as a volume in compose.
RUN mkdir -p /app/data

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status==200 else 1)"

CMD ["uvicorn", "munshiji.main:app", "--host", "0.0.0.0", "--port", "8000"]
