FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    PIP_TRUSTED_HOST=mirrors.aliyun.com \
    PIP_DEFAULT_TIMEOUT=120 \
    RDS_COPILOT_LOG_FILE=/data/rds-copilot.log \
    RDS_COPILOT_CONVERSATION_STORE_FILE=/data/copilot_conversations.json

WORKDIR /app

RUN set -eux; \
    if [ -f /etc/apt/sources.list ]; then \
        sed -i \
            -e 's|http://deb.debian.org/debian|http://mirrors.aliyun.com/debian|g' \
            -e 's|http://security.debian.org/debian-security|http://mirrors.aliyun.com/debian-security|g' \
            -e 's|http://deb.debian.org/debian-security|http://mirrors.aliyun.com/debian-security|g' \
            /etc/apt/sources.list; \
    fi; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i \
            -e 's|http://deb.debian.org/debian|http://mirrors.aliyun.com/debian|g' \
            -e 's|http://security.debian.org/debian-security|http://mirrors.aliyun.com/debian-security|g' \
            -e 's|http://deb.debian.org/debian-security|http://mirrors.aliyun.com/debian-security|g' \
            /etc/apt/sources.list.d/debian.sources; \
    fi; \
    pip config set global.index-url "$PIP_INDEX_URL"; \
    pip config set global.trusted-host "$PIP_TRUSTED_HOST"; \
    useradd --uid 1000 --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data

COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install -r requirements.txt \
    && pip install tzdata

COPY --chown=appuser:appuser . .

ENTRYPOINT ["python", "/app/docker/entrypoint.py"]
CMD ["python", "main.py"]
