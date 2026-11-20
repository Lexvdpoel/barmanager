FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PUID=99 PGID=100 TZ=Europe/Amsterdam \
    BM_HOST=0.0.0.0 BM_PORT=8080 \
    BM_DB_PATH=/config/barmanager.db BM_IMAGE_DIR=/config/images

WORKDIR /src
COPY pyproject.toml README.md ./
COPY barmanager ./barmanager
RUN pip install --no-cache-dir . && rm -rf /src
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod 755 /entrypoint.sh
WORKDIR /config

VOLUME /config
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('BM_PORT', '8080'))"
ENTRYPOINT ["/entrypoint.sh"]
CMD ["barmanager"]
