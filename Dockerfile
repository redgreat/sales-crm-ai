# sales-crm-ai 多阶段构建镜像
# 阶段1: 前端编译（Node.js）
FROM node:24-alpine AS frontend
WORKDIR /build/frontend
COPY frontend/package*.json ./
RUN npm ci --legacy-peer-deps
COPY frontend/ .
RUN npx svelte-kit sync && npx vite build

# 阶段2: Python 运行时
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock

COPY app ./app
COPY scripts ./scripts

# 复制前端构建产物
COPY --from=frontend /build/frontend/.svelte-kit/output ./frontend-dist

RUN mkdir -p conf .local && chown -R nobody:nogroup /app
USER nobody

EXPOSE 8310

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; from app.config import get_settings; s = get_settings(); urllib.request.urlopen(f'http://127.0.0.1:{s.api.port}/health', timeout=2)"

CMD ["python", "scripts/serve.py", "--host", "0.0.0.0"]
