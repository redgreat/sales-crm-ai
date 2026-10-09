#!/usr/bin/env bash
#
# sales-crm-ai 镜像仓同步脚本：按需把代码推送到私有 GitLab（gitlab.lunz.cn）。
# 主仓库仍是 GitHub；本脚本只新增/使用名为 gitlab 的 remote，不改动 origin。
# 首次: ./scripts/sync-gitlab.sh -u https://gitlab.lunz.cn/<组>/sales-crm-ai.git
# 之后: ./scripts/sync-gitlab.sh                # 推送当前分支
#       ./scripts/sync-gitlab.sh -t             # 推送当前分支 + 全部标签
#       ./scripts/sync-gitlab.sh -A -t          # 同步全部分支 + 全部标签
#       ./scripts/sync-gitlab.sh -p             # 从 GitLab 拉取远端分支并合并到当前分支

set -eo pipefail

URL=""
BRANCH_ARG=""
USERNAME_ARG=""
PULL=0
ALL=0
TAGS=0
FORCE=0
DRYRUN=0

show_usage() {
  cat <<'EOF'
用法: ./scripts/sync-gitlab.sh [选项] [仓库地址]
首次: ./scripts/sync-gitlab.sh -u https://gitlab.lunz.cn/<组>/sales-crm-ai.git
之后: ./scripts/sync-gitlab.sh                # 推送当前分支
      ./scripts/sync-gitlab.sh -t             # 推送当前分支 + 全部标签
      ./scripts/sync-gitlab.sh -A -t          # 同步全部分支 + 全部标签
      ./scripts/sync-gitlab.sh -p             # 从 GitLab 拉取远端分支并合并到当前分支

选项:
  -u, --url <url>      GitLab 仓库地址（首次需要；保存为 gitlab remote，重复传入且不同则更新）
  -b, --branch <name>  要推送的分支（默认当前分支）
  -p, --pull           拉取合并模式：fetch GitLab 远端分支并 merge 到当前分支（不含推送）
  -U, --username <s>   GitLab 用户名（默认: GITLAB_USERNAME 环境变量 > git config gitlab.username > 提示输入）
  -A, --all            推送全部分支（与 -b 互斥）
  -t, --tags           同时推送全部标签
  -f, --force          远端与本地分叉时覆盖（先 fetch 再 --force-with-lease，安全覆盖）
  -n, --dryrun         只打印将执行的动作，不添加 remote、不推送
  -h, --help           显示帮助

认证（二选一）:
  1) 默认: 走 Git 凭据管理器/终端提示——首次输入后可保存。
  2) 显式: 设置环境变量 GITLAB_USERNAME / GITLAB_PASSWORD，仅本次运行使用、不落盘。
     账号开启 2FA 时密码请改用 Personal Access Token（scope: write_repository）。

说明: 主仓库仍为 GitHub；本脚本只操作名为 gitlab 的 remote，不影响 origin。
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    -u|--url)      URL="${2:-}"; shift 2 ;;
    -b|--branch)   BRANCH_ARG="${2:-}"; shift 2 ;;
    -p|--pull)     PULL=1; shift ;;
    -U|--username) USERNAME_ARG="${2:-}"; shift 2 ;;
    -A|--all)      ALL=1; shift ;;
    -t|--tags)     TAGS=1; shift ;;
    -f|--force)    FORCE=1; shift ;;
    -n|--dryrun)   DRYRUN=1; shift ;;
    -h|--help)     show_usage; exit 0 ;;
    -*)            echo "✖ 未知参数: $1" >&2; show_usage; exit 1 ;;
    *)             URL="$1"; shift ;;
  esac
done

if [ -t 1 ]; then
  C_INFO=$'\033[34m'; C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_OFF=$'\033[0m'
else
  C_INFO=""; C_OK=""; C_WARN=""; C_ERR=""; C_OFF=""
fi
info()    { printf '%s➤ %s%s\n' "$C_INFO" "$*" "$C_OFF"; }
success() { printf '%s✔ %s%s\n' "$C_OK" "$*" "$C_OFF"; }
warn()    { printf '%s⚠ %s%s\n' "$C_WARN" "$*" "$C_OFF"; }
error()   { printf '%s✖ %s%s\n' "$C_ERR" "$*" "$C_OFF" >&2; }
die()     { error "$*"; exit 1; }

strip_userinfo() { printf '%s' "$1" | sed -E 's#^(https?://)[^/@]+@#\1#'; }

# 1) 确认位于 Git 仓库
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  die '当前目录不是 Git 仓库。'
fi

# 2) 解析 gitlab remote（首次需要 -u 或交互输入）
remote_url=""
if git remote | grep -qx gitlab; then
  remote_url="$(git remote get-url gitlab)"
fi

if [ -n "$URL" ]; then
  normalized="$(strip_userinfo "$URL")"
  if [ "$normalized" != "$URL" ]; then
    warn 'remote 地址包含账号信息，已移除（凭据请用凭据管理器或环境变量）。'
    URL="$normalized"
  fi
  if [ -z "$remote_url" ]; then
    if [ "$DRYRUN" -eq 1 ]; then
      info "[DryRun] 将添加 remote 'gitlab' -> $URL"
    else
      git remote add gitlab "$URL" || die 'git remote add 失败。'
      success "已添加 remote gitlab -> $URL"
    fi
    remote_url="$URL"
  elif [ "$remote_url" != "$URL" ]; then
    if [ "$DRYRUN" -eq 1 ]; then
      info "[DryRun] 将更新 remote 'gitlab': $remote_url -> $URL"
    else
      git remote set-url gitlab "$URL" || die 'git remote set-url 失败。'
      success "remote gitlab 已更新 -> $URL"
    fi
    remote_url="$URL"
  fi
elif [ -z "$remote_url" ]; then
  if [ "$DRYRUN" -eq 1 ]; then
    die '尚未配置 gitlab remote：请传入 -u <仓库地址>'
  fi
  printf '首次同步：输入 GitLab 仓库地址（如 https://gitlab.lunz.cn/<组>/sales-crm-ai.git）: '
  read -r URL
  if [ -z "$(printf '%s' "$URL" | tr -d '[:space:]')" ]; then
    die '未输入地址，已取消。'
  fi
  URL="$(strip_userinfo "$URL")"
  git remote add gitlab "$URL" || die 'git remote add 失败。'
  success "已添加 remote gitlab -> $URL"
  remote_url="$URL"
fi

# 3) 推送/拉取范围
if [ "$PULL" -eq 1 ]; then
  if [ "$ALL" -eq 1 ]; then die '-p/--pull 与 -A/--all 互斥，二选一。'; fi
  if [ "$TAGS" -eq 1 ]; then warn '-p/--pull 模式忽略 -t/--tags。'; TAGS=0; fi
  if [ "$FORCE" -eq 1 ]; then warn '-p/--pull 模式忽略 -f/--force。'; FORCE=0; fi
fi
if [ "$ALL" -eq 1 ] && [ -n "$BRANCH_ARG" ]; then
  die '-A/--all 与 -b/--branch 互斥，二选一。'
fi
BRANCH="$BRANCH_ARG"
if [ "$ALL" -eq 0 ]; then
  if [ -z "$BRANCH" ]; then
    BRANCH="$(git symbolic-ref --short -q HEAD || true)"
  fi
  if [ -z "$BRANCH" ]; then
    die 'HEAD 处于分离状态：请用 -b 指定分支。'
  fi
fi

# 4) 认证方式（显式 or Git 凭据管理器）
AUTH=()
auth_mode='Git 凭据管理器（首次可能提示输入，可保存）'
explicit=0
if [ -n "${GITLAB_PASSWORD:-}" ] || [ -n "${GITLAB_USERNAME:-}" ] || [ -n "$USERNAME_ARG" ]; then
  explicit=1
  auth_mode='显式认证（仅本次运行，不落盘）'
fi

if [ "$ALL" -eq 1 ]; then
  scope='全部分支'
else
  scope="$BRANCH"
fi
if [ "$TAGS" -eq 1 ]; then
  scope="$scope + 全部标签"
fi

if [ "$DRYRUN" -eq 1 ]; then
  if [ "$PULL" -eq 1 ]; then mode='拉取合并（fetch + merge）'; else mode='推送（push）'; fi
  if [ "$FORCE" -eq 1 ]; then force_desc='是（先 fetch 再 --force-with-lease）'; else force_desc='否'; fi
  info "[DryRun] 模式     : $mode"
  info "[DryRun] 目标     : gitlab -> $remote_url"
  info "[DryRun] 范围     : $scope"
  if [ "$PULL" -eq 0 ]; then info "[DryRun] 强制覆盖 : $force_desc"; fi
  info "[DryRun] 认证方式 : $auth_mode"
  info '[DryRun] 未执行任何变更。'
  exit 0
fi

# 5) 显式认证：解析用户名/密码
if [ "$explicit" -eq 1 ]; then
  user="$USERNAME_ARG"
  if [ -z "$user" ]; then user="${GITLAB_USERNAME:-}"; fi
  if [ -z "$user" ]; then user="$(git config --local --get gitlab.username 2>/dev/null || true)"; fi
  if [ -z "$user" ]; then
    printf 'GitLab 用户名: '
    read -r user
    if [ -z "$(printf '%s' "$user" | tr -d '[:space:]')" ]; then die '未输入用户名，已取消。'; fi
    if ! git config --local gitlab.username "$user"; then
      warn '保存用户名到 git config 失败（下次可用 -U/--username 指定）。'
    fi
  fi
  pass="${GITLAB_PASSWORD:-}"
  if [ -z "$pass" ]; then
    printf 'GitLab 密码/令牌（输入不回显）: '
    read -rs pass
    printf '\n'
  fi
  if [ -z "$pass" ]; then die '未提供密码/令牌，已取消。'; fi
  basic="$(printf '%s:%s' "$user" "$pass" | base64 | tr -d '\n')"
  AUTH=(-c credential.helper= -c "http.extraheader=Authorization: Basic $basic")
  pass=""
fi

# 6) 拉取合并模式：fetch + merge 后即结束（推回请再执行一次同步）
if [ "$PULL" -eq 1 ]; then
  if [ -n "$(git status --porcelain)" ]; then
    die '工作区有未提交改动：请先 commit 或 stash 后再拉取合并。'
  fi
  info '拉取 gitlab 远端状态...'
  if ! git "${AUTH[@]}" fetch gitlab; then
    die 'git fetch gitlab 失败（网络或认证问题）。'
  fi
  remote_ref="gitlab/$BRANCH"
  if ! git rev-parse --verify -q "$remote_ref" >/dev/null; then
    die "远端分支不存在: $remote_ref"
  fi
  behind="$(git rev-list --left-right --count "HEAD...$remote_ref" | awk '{print $2}')"
  if [ "$behind" -eq 0 ]; then
    success "GitLab 侧无新提交，$BRANCH 已是最新。"
    exit 0
  fi
  info "合并 $remote_ref（本地落后 $behind 个提交）..."
  if ! git merge --no-edit "$remote_ref"; then
    if [ -f "$(git rev-parse --absolute-git-dir)/MERGE_HEAD" ]; then
      error '合并存在冲突：请解决冲突后 git add <文件> && git commit 完成合并；或 git merge --abort 放弃。'
    else
      error '合并失败。'
    fi
    exit 1
  fi
  success "已合并 $remote_ref -> $BRANCH"
  info '如需推回 GitLab：再执行一次本脚本（不带 -p/--pull）。'
  exit 0
fi

# 7) 强制覆盖前先同步远端状态（--force-with-lease 依赖 remote-tracking）
if [ "$FORCE" -eq 1 ]; then
  info '拉取 gitlab 远端状态（--force-with-lease 前置）...'
  if ! git "${AUTH[@]}" fetch gitlab; then
    die 'git fetch gitlab 失败（网络或认证问题）。'
  fi
fi

# 8) 推送
push_args=(push)
if [ "$FORCE" -eq 1 ]; then push_args+=(--force-with-lease); fi
if [ "$ALL" -eq 1 ]; then push_args+=(--all); fi
if [ "$TAGS" -eq 1 ]; then push_args+=(--tags); fi
push_args+=(gitlab)
if [ "$ALL" -eq 0 ]; then push_args+=("$BRANCH"); fi

info "推送 $scope 到 gitlab..."
push_out=""
push_exit=0
push_out="$(git "${AUTH[@]}" "${push_args[@]}" 2>&1)" || push_exit=$?
if [ -n "$push_out" ]; then printf '%s\n' "$push_out"; fi
if [ "$push_exit" -ne 0 ]; then
  error "推送失败（git 退出码 $push_exit）。"
  settings_hint=""
  case "$remote_url" in
    https://*.git|http://*.git) settings_hint="${remote_url%.git}/-/settings/repository" ;;
  esac
  group_hint=""
  case "$remote_url" in
    https://*/*/*|http://*/*/*)
      group_hint="${remote_url%.git}"
      group_hint="${group_hint%/*}/-/settings/repository"
      ;;
  esac
  if printf '%s' "$push_out" | grep -qiE 'pre-receive hook declined|protected'; then
    warn '受保护分支被拒（GitLab protected branch）：空仓库的默认分支受保护、推送账号角色不足（与分支名无关，换 main/master 都会拒）。'
    warn '  方案1: 先确认推送用的是本人账号：凭据管理器可能缓存了其他账号（可删掉 git https://<host> 条目重新弹窗登录）。'
    warn '  方案2: 打开 Members 页核对角色；Developer 需 Maintainer/Owner 提升角色，或由其放开保护。'
    warn '  方案3: 有 Maintainer 权限时，在 Protected branches 将默认分支 Allowed to push 设为 "Developers and Maintainers"。'
    warn '  方案4: 请 Maintainer/Owner 推送初始提交（仅解除本次，后续推送仍需方案2/3）。'
    if [ -n "$settings_hint" ]; then warn "  项目设置: $settings_hint"; fi
    if [ -n "$group_hint" ]; then warn "  组级设置（项目设置被锁定或无权限时）: $group_hint"; fi
  fi
  if printf '%s' "$push_out" | grep -qiE 'non-fast-forward|fetch first'; then
    warn '若是非快进/分叉（远端初始化过 README、或远端有本地之外的提交）：加 -f/--force 覆盖。'
  fi
  if printf '%s' "$push_out" | grep -qiE 'Authentication failed|could not read Username|returned error: 40[13]|Invalid username or password|access denied'; then
    warn '认证失败：设置 GITLAB_USERNAME / GITLAB_PASSWORD，或让凭据管理器保存；账号开启 2FA 时密码改用 Personal Access Token（scope: write_repository）。'
  fi
  if printf '%s' "$push_out" | grep -qiE 'SSL|certificate'; then
    warn '证书错误：检查 gitlab.lunz.cn 证书链；临时可用 git config --local http.https://gitlab.lunz.cn.sslVerify false（不建议长期关闭）。'
  fi
  exit 1
fi
success "同步完成：$scope -> gitlab（$remote_url）"
