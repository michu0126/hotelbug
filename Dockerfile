FROM python:3.12-slim AS api
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Asia/Shanghai
WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 hotelbug \
    && useradd --uid 10001 --gid hotelbug --no-create-home hotelbug \
    && mkdir -p /data /logs && chown hotelbug:hotelbug /data /logs
COPY --chown=hotelbug:hotelbug backend/ /app/
USER hotelbug
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.api.main:app --host 0.0.0.0 --port 8000 --no-access-log"]

FROM api AS worker
USER root
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright DISPLAY=:99
RUN pip install --no-cache-dir -r requirements-browser.txt \
    && python -m playwright install --with-deps chromium \
    && apt-get update \
    && apt-get install -y --no-install-recommends xvfb \
    && apt-get clean \
    && mkdir -p /home/hotelbug \
    && usermod -d /home/hotelbug hotelbug \
    && chown -R hotelbug:hotelbug /ms-playwright /home/hotelbug
COPY --chown=hotelbug:hotelbug docker/worker-entrypoint.sh /app/worker-entrypoint.sh
USER hotelbug
ENTRYPOINT ["sh", "/app/worker-entrypoint.sh"]
CMD ["python", "-m", "app.crawler.worker"]

FROM node:22-alpine AS frontend-build
WORKDIR /src
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM nginx:1.28-alpine AS frontend
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=frontend-build /src/dist /usr/share/nginx/html
EXPOSE 8080
