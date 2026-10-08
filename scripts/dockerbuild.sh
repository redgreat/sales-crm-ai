#!/usr/bin/env bash
#
# sales-crm-ai 镜像发布脚本（bash 版）：自动计算（或指定）下一个 v* Git 标签并推送，
# 由 CI（.github/workflows/ci.yml）监听标签触发多阶段构建，
# 将镜像发布到 GHCR（ghcr.io/redgreat/sales-crm-ai）与 Quay（quay.io/zrcrm/sales-crm-ai）。
#
# 用法: ./scripts/dockerbuild.sh [版本标签]
# 示例: ./scripts/dockerbuild.sh v0.0.1

set -euo pipefail

VERSION=""
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
用法: ./scripts/dockerbuild.sh [版本标签]
示例: ./scripts/dockerbuild.sh v0.0.1

参数:
  [版本标签]   Git 标签版本 (留空则自动计算，如 v1.2.3 -> v1.2.4)
  -h, --help   显示帮助

说明: 脚本只负责打标签并推送；推送标签后 CI 自动构建镜像并发布到
      ghcr.io/redgreat/sales-crm-ai 与 quay.io/zrcrm/sales-crm-ai。
EOF
}

for arg in "$@"; do
  case "$arg" in
    -h|--help) HELP=1 ;;
    -*) error "未知参数: $arg"; show_usage; exit 1 ;;
    *) VERSION="$arg" ;;
  esac
done
if [ "$HELP" -eq 1 ]; then show_usage; exit 0; fi

ensure_git() {
  if ! command -v git >/dev/null 2>&1; then
    error '未找到 git 命令，请先安装 Git 并确保在 PATH 中。'
    exit 1
  fi
}

ensure_clean_working_tree() {
  local changes answer
  changes="$(git status --porcelain || true)"
  if [ -n "$changes" ]; then
    warn "检测到未提交/已暂存/未跟踪的改动：
$changes"
    printf '仍要继续打标签并推送吗？输入 YES 继续，其它任意输入中止 '
    read -r answer
    if [ "$(printf '%s' "$answer" | tr '[:lower:]' '[:upper:]')" != "YES" ]; then
      error '已取消打标签与推送。'
      exit 1
    fi
    warn '已确认忽略当前改动，将继续打标签与推送。'
  fi
}

# 获取最新标签（按语义版本排序）
get_latest_tag() {
  git tag --list 'v*' --sort=-version:refname | head -n 1 || true
}

# 将尾数 +1：优先识别 vMAJOR.MINOR.PATCH，否则对末尾数字增量
bump_tail() {
  local tag="$1" prefix n
  if [ -z "${tag// }" ]; then echo "v0.0.1"; return; fi
  if [[ "$tag" =~ ^v([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]; then
    echo "v${BASH_REMATCH[1]}.${BASH_REMATCH[2]}.$(( ${BASH_REMATCH[3]} + 1 ))"
  elif [[ "$tag" =~ ^(.*[^0-9])?([0-9]+)$ ]]; then
    prefix="${BASH_REMATCH[1]:-}"
    n="${BASH_REMATCH[2]}"
    echo "${prefix}$(( n + 1 ))"
  else
    echo "${tag}-1"
  fi
}

ensure_git
ensure_clean_working_tree

# 若未显式传入版本参数，则依据最新标签自动计算
if [ -z "$VERSION" ]; then
  latest="$(get_latest_tag)"
  if [ -n "$latest" ]; then
    info "检测到当前最新标签: $latest"
    VERSION="$(bump_tail "$latest")"
    info "自动计算版本: $VERSION"
  else
    warn '未发现任何标签，使用默认 v0.0.1'
    VERSION="v0.0.1"
  fi
fi

printf '%s\n' "=============================================="
info "Docker Tag 发布脚本  版本: $VERSION"
printf '%s\n' "=============================================="

info "创建 Git 标签 $VERSION"
if [ -z "$(git tag -l "$VERSION")" ]; then
  branch="$(git branch --show-current)"
  git tag "$VERSION"
  success "标签 $VERSION 创建成功（分支 $branch）"
else
  warn "标签 $VERSION 已存在，跳过创建"
fi

info "推送标签到远程 origin"
git push origin "$VERSION"
success "标签 $VERSION 推送完成"
info 'CI 已触发：测试通过后自动构建镜像并推送 GHCR 与 Quay。'
