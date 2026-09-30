# One image recipe for every Python microservice + the migration job. Build context = repo root.
#   docker build -f infra/docker/python-service.Dockerfile --target service --build-arg SERVICE=advisor .
#
# Disk-friendly layering: all third-party libraries are installed ONCE in the `deps` stage, from
# files that rarely change (fm_common's pyproject.toml + requirements.txt). Every service image
# and the migrate image reuse those same layers, so Docker stores them once (~1 GB instead of
# ~6 GB), and a code change only rebuilds the few-MB layers at the end.

FROM python:3.12-slim AS deps
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 app
WORKDIR /app
COPY libs/fm_common/pyproject.toml /tmp/fmc/pyproject.toml
COPY requirements.txt /tmp/requirements.txt
# install fm_common's *dependencies* via a stub package, then every service's libraries
RUN mkdir -p /tmp/fmc/fm_common && touch /tmp/fmc/fm_common/__init__.py \
 && pip install /tmp/fmc && pip uninstall -y fm-common \
 && grep -vE '^\s*-e' /tmp/requirements.txt > /tmp/req.txt && pip install -r /tmp/req.txt \
 && rm -rf /tmp/fmc /tmp/req.txt /tmp/requirements.txt
# the shared library's code (small layer; rebuilt when it changes)
COPY libs/fm_common /app/libs/fm_common
RUN pip install --no-deps /app/libs/fm_common

FROM deps AS migrate
COPY services /app/services
COPY migrations /app/migrations
WORKDIR /app/migrations
CMD ["alembic", "upgrade", "head"]

FROM deps AS service
ARG SERVICE
COPY services/${SERVICE} /app/services/${SERVICE}
WORKDIR /app/services/${SERVICE}
USER app
ENV SERVICE=${SERVICE}
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s CMD curl -fsS http://localhost:8000/health/live || exit 1
# uvloop event loop + httptools parser; one process per container (scale with replicas)
CMD ["sh", "-c", "exec uvicorn ${SERVICE}_svc.main:app --host 0.0.0.0 --port 8000 --loop uvloop --http httptools --proxy-headers --forwarded-allow-ips='*' --no-access-log"]
