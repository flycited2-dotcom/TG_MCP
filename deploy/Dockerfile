FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg espeak-ng libreoffice-writer libreoffice-impress ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 telegram-mcp \
    && useradd --uid 10001 --gid 10001 --no-create-home \
       --home-dir /nonexistent --shell /usr/sbin/nologin telegram-mcp

# Explicit copies keep local sessions, exports and credentials out of the image.
COPY server.py telegram_backend.py oauth_owner.py file_downloads.py ./
COPY tool_models.py file_store.py action_store.py extended_backend.py \
     media_processing.py upload_routes.py extension_tools.py ./

USER 10001:10001
EXPOSE 8000
CMD ["python", "server.py"]
