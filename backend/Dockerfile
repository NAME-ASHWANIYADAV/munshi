# MunshiJi API container image.
#
# Used by `docker compose --profile full up` locally, and by any host that builds from this
# directory. The normal development path is still the virtualenv (scripts/setup.ps1).

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

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import os,urllib.request,sys; p=os.getenv('PORT','8000'); sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+p+'/api/health', timeout=4).status==200 else 1)"

# Seed on first boot, then serve.
#
# Two things a hosting platform needs that a local virtualenv does not. The database is generated
# rather than shipped, so a container starting with an empty volume has to build it before the API
# can answer anything — but only when it is missing, so a restart with a persistent disk keeps its
# history. And the listening port comes from the environment: platforms assign one and expect the
# process to honour it rather than picking its own.
CMD ["sh", "-c", "test -f /app/data/munshiji.db || python -m munshiji.cli seed --reset; exec uvicorn munshiji.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
