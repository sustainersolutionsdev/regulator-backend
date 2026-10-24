# Regulator backend image (OPEN-11). One image, two Cloud Run services:
#   regulator-api            APP_MODULE=main:app    (default)
#   regulator-alerts-worker  APP_MODULE=worker:app  (Pub/Sub push target, TAD Sec. 9)
# Built and deployed by cloudbuild.deploy-dev.yaml. No credentials are baked in:
# on Cloud Run each service runs as its own service account (TAD 13.1).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    APP_MODULE=main:app

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY core ./core
COPY main.py worker.py ./
# Compile the app's own code at build time (Oct 23, cold start). The image runs
# with PYTHONDONTWRITEBYTECODE and as a user who can't write to /app, so
# without this every new server recompiled it on start. pip already does the
# same for the installed libraries.
RUN python -m compileall -q core main.py worker.py

RUN useradd --create-home --uid 10001 app
USER app

CMD exec uvicorn "$APP_MODULE" --host 0.0.0.0 --port "$PORT" --proxy-headers --forwarded-allow-ips="*"
