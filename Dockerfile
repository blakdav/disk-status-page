FROM python:3.13-slim-bookworm
LABEL org.opencontainers.image.source="https://github.com/blakdav/disk-status-page"
RUN apt-get update \
    && apt-get install -y --no-install-recommends hdparm smartmontools \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY app.py disks.example.json backups.example.json ./
COPY static ./static
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data
EXPOSE 8080
VOLUME ["/data"]
HEALTHCHECK --interval=60s --timeout=5s --start-period=180s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"
CMD ["python", "app.py"]
