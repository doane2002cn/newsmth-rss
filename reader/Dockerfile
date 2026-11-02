FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DISPLAY=:99 \
    READER_DATA_DIR=/data \
    VNC_HTTP_URL=http://127.0.0.1:6081 \
    VNC_WEBSOCKET_URL=ws://127.0.0.1:6081/websockify

RUN sed -i \
        -e 's|azure.archive.ubuntu.com|mirrors.aliyun.com|g' \
        -e 's|security.ubuntu.com|mirrors.aliyun.com|g' \
        /etc/apt/sources.list.d/ubuntu.sources \
    && apt-get -o Acquire::Retries=5 update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        curl \
        novnc \
        scrot \
        tini \
        websockify \
        x11-utils \
        x11vnc \
        xvfb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --ignore-installed -r /app/requirements.txt

COPY app /app/app
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod 0755 /app/entrypoint.sh \
    && mkdir -p /data \
    && chown -R pwuser:pwuser /data

USER pwuser
EXPOSE 8010

HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
    CMD curl --fail --silent \
        "http://${READER_BIND_HOST:-127.0.0.1}:8010/healthz" >/dev/null || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]
