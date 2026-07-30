# Build context is the repository root, so `coaching/` can be copied alongside the
# agent. Referenced from docker-compose.yml as `dockerfile: agent/Dockerfile`.
FROM docker.io/python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # `coaching` and `agent` are both flat module directories rather than installed
    # packages, so both go on the path.
    PYTHONPATH=/app/agent:/app/coaching

WORKDIR /app

COPY agent/requirements.txt agent/requirements.txt
RUN pip install --no-cache-dir -r agent/requirements.txt

COPY coaching/ coaching/
COPY agent/ agent/
# The ETL and sidecar loader run inside this container (see
# deploy/truenas/SETUP.md): the TrueNAS host has no place to pip-install
# openpyxl that survives an OS upgrade.
COPY etl/ etl/
COPY sidecar/ sidecar/

# Run as a non-root user. UID 1000 matches the ownership the TrueNAS setup notes
# apply to the bind-mounted datasets.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

CMD ["uvicorn", "main:app", "--app-dir", "/app/agent", "--host", "0.0.0.0", "--port", "8000"]
