FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    WH_HOST=0.0.0.0 \
    WH_PORT=8000

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates passwd tar tor \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium

COPY . .
RUN groupadd --system --gid 10001 signaldesk \
    && useradd --system --uid 10001 --gid signaldesk --home-dir /home/signaldesk --create-home signaldesk \
    && mkdir -p /app/data /app/results /app/.thumb_cache /app/work /app/run_artifact \
    && chown -R signaldesk:signaldesk /app /home/signaldesk

USER 10001:10001
EXPOSE 8000
CMD ["python", "app.py"]
