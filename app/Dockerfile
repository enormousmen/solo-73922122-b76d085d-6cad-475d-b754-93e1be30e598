# API 镜像，构建上下文为仓库根目录（见 docker-compose.yml）
FROM python:3.12-slim

WORKDIR /srv

COPY app/requirements.txt app/requirements.txt
RUN pip install --no-cache-dir -r app/requirements.txt

COPY app/ app/
COPY config/ config/

ENV APP_CONFIG=/srv/config/servers.json \
    PYTHONUNBUFFERED=1

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
