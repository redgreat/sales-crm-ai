# sales-crm-ai 多阶段构建镜像
# 阶段1: 配置后台前端编译（Node.js）→ 静态 SPA 产物 frontend/build
FROM node:24-alpine AS frontend
WORKDIR /build/frontend
COPY frontend/package*.json ./
RUN npm ci --legacy-peer-deps
COPY frontend/ .
RUN npx svelte-kit sync && npx vite build && test -f build/index.html

# 阶段2: Python 运行时
FROM python:3.12-slim

ARG S6_OVERLAY_VERSION=3.2.3.2
ARG TARGETARCH

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl xz-utils \
    && case "$TARGETARCH" in amd64) s6_arch=x86_64 ;; arm64) s6_arch=aarch64 ;; *) exit 1 ;; esac \
    && for artifact in noarch "$s6_arch"; do \
         curl -fsSL --retry 3 "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-${artifact}.tar.xz" -o "/tmp/s6-overlay-${artifact}.tar.xz" \
         && curl -fsSL --retry 3 "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-${artifact}.tar.xz.sha256" -o "/tmp/s6-overlay-${artifact}.tar.xz.sha256" \
         && (cd /tmp && sha256sum -c "s6-overlay-${artifact}.tar.xz.sha256") \
         && tar -C / -Jxpf "/tmp/s6-overlay-${artifact}.tar.xz"; \
       done \
    && rm -f /tmp/s6-overlay-*.tar.xz /tmp/s6-overlay-*.sha256 \
    && apt-get purge -y --auto-remove curl xz-utils \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock

COPY app ./app
COPY scripts/serve.py scripts/init_db.py scripts/init_checkpoints.py ./scripts/
COPY deploy/s6/ai-api/ /etc/s6-overlay/s6-rc.d/ai-api/
COPY deploy/s6-user-bundles/ /etc/s6-overlay/user-bundles.d/

# 复制配置后台前端产物（FastAPI 挂在 /admin 提供，与 API 同端口）
COPY --from=frontend /build/frontend/build ./admin-dist
# H5 测试联调页（单文件静态页，随镜像由 AI 服务挂在 /h5 提供）
COPY tests/h5/index.html ./h5-dist/index.html

RUN mkdir -p conf .local && chown -R nobody:nogroup /app \
    && chmod +x /etc/s6-overlay/s6-rc.d/ai-api/run

EXPOSE 8310

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; from app.config import get_settings; s = get_settings(); urllib.request.urlopen(f'http://127.0.0.1:{s.api.port}/ready', timeout=2)"

ENTRYPOINT ["/init"]
