FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir playwright

WORKDIR /app
COPY download_albums.py /app/download_albums.py

RUN useradd --uid 1000 --create-home downloader \
    && mkdir -p /downloads \
    && chown -R downloader:downloader /app /downloads

USER 1000:1000
ENTRYPOINT ["python", "/app/download_albums.py"]
