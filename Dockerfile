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
