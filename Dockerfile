FROM python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DEFAULT_TIMEOUT=300 \
    PIP_RETRIES=10
RUN apt-get update && apt-get install -y --no-install-recommends build-essential curl libpq5 && rm -rf /var/lib/apt/lists/*
RUN python -m pip install --no-cache-dir pip==26.1.2
COPY pyproject.toml requirements.lock /app/
COPY app /app/app
COPY scripts /app/scripts
RUN python -m pip install --no-cache-dir -c requirements.lock .
COPY alembic.ini /app/
COPY alembic /app/alembic
COPY branding /app/branding
RUN adduser --disabled-password --gecos "" appuser \
    && mkdir -p /app/uploads \
    && chown -R appuser:appuser /app
USER appuser
