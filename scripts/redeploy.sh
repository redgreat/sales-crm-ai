#!/usr/bin/env bash
#
# sales-crm-ai 重新部署脚本（在目标服务器上执行）：
#   停止并移除旧容器 → 删除本地旧镜像 → 拉取最新镜像 → 后台启动 → 健康检查。
#
# 镜像来源见 docker-compose.yml：quay.io/zrcrm/sales-crm-ai（标签默认 latest）。
# 镜像由 CI 监听 v* 标签构建发布，打标签推送用 scripts/dockerbuild.sh。
#
# 用法: ./scripts/redeploy.sh [选项]
#   --dir <路径>     部署目录（默认 /root/sales-crm-ai，可用环境变量 SAI_DEPLOY_DIR 覆盖）
#   --tag <标签>     镜像标签（默认 latest，导出为 SAI_IMAGE_TAG 供 compose 插值）
#   --migrate        同时应用数据库迁移（init_db.py --apply + init_checkpoints.py --apply）
#   --no-prune       跳过清理悬空镜像（悬空镜像指 <none> 标签的旧层）
#   --wait <秒>      健康检查最长等待时间（默认 60 秒，0 表示不等待）
#   -h, --help       显示帮助
#
# 示例:
#   ./scripts/redeploy.sh                        # 拉最新镜像并重启
#   ./scripts/redeploy.sh --tag v1.2.3           # 部署指定版本
#   ./scripts/redeploy.sh --migrate              # 重启前应用数据库迁移

set -euo pipefail

DEPLOY_DIR="${SAI_DEPLOY_DIR:-/root/sales-crm-ai}"
IMAGE_TAG="${SAI_IMAGE_TAG:-latest}"
SERVICE="sales-crm-ai"
CONTAINER="sales-crm-ai"
PORT="${SAI_PORT:-8310}"
MIGRATE=0
PRUNE=1
WAIT_SECONDS=60
HELP=0

if [ -t 1 ]; then
  C_INFO=$'\033[34m'; C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_OFF=$'\033[0m'
else
  C_INFO=""; C_OK=""; C_WARN=""; C_ERR=""; C_OFF=""
fi

info()    { printf '%s➤ %s%s\n' "$C_INFO" "$*" "$C_OFF"; }
success() { printf '%s✔ %s%s\n' "$C_OK" "$*" "$C_OFF"; }
warn()    { printf '%s⚠ %s%s\n' "$C_WARN" "$*" "$C_OFF"; }
error()   { printf '%s✖ %s%s\n' "$C_ERR" "$*" "$C_OFF" >&2; }

show_usage() {
  cat <<'EOF'
用法: ./scripts/redeploy.sh [选项]

选项:
  --dir <路径>     部署目录（默认 /root/sales-crm-ai，或环境变量 SAI_DEPLOY_DIR）
  --tag <标签>     镜像标签（默认 latest）
  --migrate        重启前应用数据库迁移（init_db + init_checkpoints）
  --no-prune       跳过清理悬空镜像
  --wait <秒>      健康检查最长等待时间（默认 60，0 表示不等待）
  -h, --help       显示帮助

说明: 脚本只做「拉新镜像 + 重启」，不会自动改库；需要迁移时显式加 --migrate。
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dir)   DEPLOY_DIR="${2:-}"; shift 2 ;;
    --tag)   IMAGE_TAG="${2:-}"; shift 2 ;;
    --wait)  WAIT_SECONDS="${2:-}"; shift 2 ;;
    --migrate) MIGRATE=1; shift ;;
    --no-prune) PRUNE=0; shift ;;
    -h|--help) HELP=1; shift ;;
    -*) error "未知参数: $1"; show_usage; exit 1 ;;
    *) error "多余参数: $1"; show_usage; exit 1 ;;
  esac
done
if [ "$HELP" -eq 1 ]; then show_usage; exit 0; fi

# docker compose（v2 插件）优先，老环境回落到 docker-compose（v1）
if docker compose version >/dev/null 2>&1; then
  DC="docker compose"
else
  DC="docker-compose"
fi
export SAI_IMAGE_TAG="$IMAGE_TAG"

printf '%s\n' "=========================================="
info "开始重新部署 sales-crm-ai 容器环境"
printf '%s\n' "=========================================="
info "部署目录: $DEPLOY_DIR"
info "镜像标签: $IMAGE_TAG"

cd "$DEPLOY_DIR" || { error "进入目录 $DEPLOY_DIR 失败，中止执行"; exit 1; }

if [ ! -f docker-compose.yml ] && [ ! -f docker-compose.yaml ]; then
  error "当前目录没有 docker-compose.yml，中止执行"
  exit 1
fi

echo "➤ 1. 停止并移除旧容器: $CONTAINER ..."
$DC stop "$SERVICE" || warn "停止容器失败或容器不存在，继续"
$DC rm -f "$SERVICE" || warn "移除容器失败或容器不存在，继续"

echo "➤ 2. 删除本地的 sales-crm-ai 镜像记录..."
docker images | grep 'sales-crm-ai' | awk '{print $3}' | xargs -r docker rmi -f || true
success "旧镜像清理完成。"

if [ "$PRUNE" -eq 1 ]; then
  echo "➤ 3. 清理悬空镜像（<none> 旧层）..."
  docker image prune -f >/dev/null 2>&1 || warn "清理悬空镜像失败，忽略"
  success "悬空镜像清理完成。"
else
  warn "3. 已跳过清理悬空镜像（--no-prune）。"
fi

echo "➤ 4. 拉取最新镜像: $IMAGE_TAG ..."
$DC pull "$SERVICE"

if [ "$MIGRATE" -eq 1 ]; then
  echo "➤ 5. 应用数据库迁移（显式授权）..."
  $DC run --rm "$SERVICE" python scripts/init_db.py --apply
  $DC run --rm "$SERVICE" python scripts/init_checkpoints.py --apply
  success "数据库迁移完成。"
else
  warn "5. 未执行数据库迁移（默认不自动改库；如需迁移加 --migrate）。"
fi

echo "➤ 6. 后台启动容器..."
$DC up -d "$SERVICE"

if [ "$WAIT_SECONDS" -gt 0 ] 2>/dev/null; then
  echo "等待服务就绪（最多 ${WAIT_SECONDS} 秒）..."
  deadline=$(( SECONDS + WAIT_SECONDS ))
  ready=0
  while [ "$SECONDS" -lt "$deadline" ]; do
    if curl -fsS "http://127.0.0.1:${PORT}/ready" >/dev/null 2>&1; then
      ready=1
      break
    fi
    sleep 2
  done
  if [ "$ready" -eq 1 ]; then
    success "服务已就绪：http://127.0.0.1:${PORT}/ready"
  else
    warn "等待超时，未通过就绪检查，请查看容器日志：docker logs --tail 100 $CONTAINER"
  fi
else
  sleep 2
fi

echo "➤ 7. 当前容器运行状态:"
docker ps -a --filter "name=$CONTAINER"

echo
info "最近 30 行容器日志:"
docker logs --tail 30 "$CONTAINER" 2>/dev/null || warn "读取容器日志失败"

printf '%s\n' "=========================================="
success "容器已更新并重启完成！后台页面: http://<host>:${PORT}/admin"
printf '%s\n' "=========================================="
