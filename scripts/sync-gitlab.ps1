#!/usr/bin/env pwsh
<#
  sales-crm-ai 镜像仓同步脚本：按需把代码推送到私有 GitLab（gitlab.lunz.cn）。
  主仓库仍是 GitHub；本脚本只新增/使用名为 gitlab 的 remote，不改动 origin。
  首次: .\scripts\sync-gitlab.ps1 -Url https://gitlab.lunz.cn/<组>/sales-crm-ai.git
  之后: .\scripts\sync-gitlab.ps1                # 推送当前分支
        .\scripts\sync-gitlab.ps1 -Tags          # 推送当前分支 + 全部标签
        .\scripts\sync-gitlab.ps1 -All -Tags     # 同步全部分支 + 全部标签
        .\scripts\sync-gitlab.ps1 -Pull          # 从 GitLab 拉取远端分支并合并到当前分支
#
# 反向操作（GitLab 侧已合入代码取回本地，人工合并后再推回 GitHub）：
#   .\scripts\pull-gitlab.ps1                     # 只拉取落地，不动工作区
#   .\scripts\pull-gitlab.ps1 -Merge -Push        # 拉取 + 合并进当前分支 + 快进推回 GitHub
#>

param(
  [string]$Url = '',
  [string]$Branch = '',
  [string]$Username = '',
  [switch]$Pull,
  [switch]$All,
  [switch]$Tags,
  [switch]$Force,
  [switch]$DryRun,
  [Alias('h')]
  [switch]$Help
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Show-Usage {
  @'
用法: .\scripts\sync-gitlab.ps1 [选项]
首次: .\scripts\sync-gitlab.ps1 -Url https://gitlab.lunz.cn/<组>/sales-crm-ai.git
之后: .\scripts\sync-gitlab.ps1                # 推送当前分支
      .\scripts\sync-gitlab.ps1 -Tags          # 推送当前分支 + 全部标签
      .\scripts\sync-gitlab.ps1 -All -Tags     # 同步全部分支 + 全部标签
      .\scripts\sync-gitlab.ps1 -Pull          # 从 GitLab 拉取远端分支并合并到当前分支

反向拉取（GitLab 已合入代码取回本地，人工合并后推回 GitHub）:
      .\scripts\pull-gitlab.ps1                # 只拉取落地到 gitlab-<branch>，不动工作区
      .\scripts\pull-gitlab.ps1 -Merge -Push   # 拉取 + 合并 + 快进推回 GitHub origin

选项:
  -Url <string>     GitLab 仓库地址（首次需要；保存为 gitlab remote，重复传入且不同则更新）
  -Branch <string>  要推送的分支（默认当前分支）
  -Pull             拉取合并模式：fetch GitLab 远端分支并 merge 到当前分支（不含推送，可与 -Branch 指定源分支）
  -All              推送全部分支（与 -Branch 互斥）
  -Tags             同时推送全部标签
  -Force            远端与本地分叉时覆盖（先 fetch 再 --force-with-lease，安全覆盖）
  -Username <s>     GitLab 用户名（默认: GITLAB_USERNAME 环境变量 > git config gitlab.username > 提示输入）
  -DryRun           只打印将执行的动作，不添加 remote、不推送
  -Help             显示帮助

认证（二选一）:
  1) 默认: 走 Git 凭据管理器——首次弹窗输入后可保存，之后免输。
  2) 显式: 设置环境变量 GITLAB_USERNAME / GITLAB_PASSWORD，仅本次运行使用、不落盘。
     账号开启 2FA 时密码请改用 Personal Access Token（scope: write_repository）。

说明: 主仓库仍为 GitHub；本脚本只操作名为 gitlab 的 remote，不影响 origin。
'@ | Write-Host
}

if ($Help) { Show-Usage; exit 0 }

# 彩色日志函数（与 dockerbuild.ps1 一致）
function Write-Info($msg)    { Write-Host "➤ $msg" -ForegroundColor Blue }
function Write-Success($msg) { Write-Host "✔ $msg" -ForegroundColor Green }
function Write-Warn($msg)    { Write-Host "⚠ $msg" -ForegroundColor Yellow }
function Write-ErrorMsg($msg){ Write-Host "✖ $msg" -ForegroundColor Red }

function Get-PlainPassword {
  $sec = Read-Host 'GitLab 密码/令牌（输入不回显）' -AsSecureString
  $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
  try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
  finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

function Remove-UrlUserInfo([string]$u) {
  # 地址里若误带了 user:pass@，剥离并告警（凭据不进 .git/config）
  if ($u -match '^(?i)(https?://)[^/@]+@(.+)$') {
    Write-Warn 'remote 地址包含账号信息，已移除（凭据请用凭据管理器或环境变量）。'
    return "$($Matches[1])$($Matches[2])"
  }
  return $u
}

try {
  # 1) 确认位于 Git 仓库
  $inside = git rev-parse --is-inside-work-tree
  if ($LASTEXITCODE -ne 0 -or "$inside".Trim() -ne 'true') {
    Write-ErrorMsg '当前目录不是 Git 仓库。'
    exit 1
  }

  # 2) 解析 gitlab remote（首次需要 -Url 或交互输入）
  $haveRemote = @((git remote)) -contains 'gitlab'
  $remoteUrl = ''
  if ($haveRemote) { $remoteUrl = ("$(git remote get-url gitlab)").Trim() }

  if ($Url) {
    $Url = Remove-UrlUserInfo $Url.Trim()
    if (-not $remoteUrl) {
      if ($DryRun) {
        Write-Info "[DryRun] 将添加 remote 'gitlab' -> $Url"
      } else {
        git remote add gitlab $Url
        if ($LASTEXITCODE -ne 0) { Write-ErrorMsg 'git remote add 失败。'; exit 1 }
        Write-Success "已添加 remote gitlab -> $Url"
      }
      $remoteUrl = $Url
    }
    elseif ($remoteUrl -ne $Url) {
      if ($DryRun) {
        Write-Info "[DryRun] 将更新 remote 'gitlab': $remoteUrl -> $Url"
      } else {
        git remote set-url gitlab $Url
        if ($LASTEXITCODE -ne 0) { Write-ErrorMsg 'git remote set-url 失败。'; exit 1 }
        Write-Success "remote gitlab 已更新 -> $Url"
      }
      $remoteUrl = $Url
    }
  }
  elseif (-not $remoteUrl) {
    if ($DryRun) {
      Write-ErrorMsg '尚未配置 gitlab remote：请传入 -Url <仓库地址>'
      exit 1
    }
    $Url = Read-Host '首次同步：输入 GitLab 仓库地址（如 https://gitlab.lunz.cn/<组>/sales-crm-ai.git）'
    if ([string]::IsNullOrWhiteSpace($Url)) { Write-ErrorMsg '未输入地址，已取消。'; exit 1 }
    $Url = Remove-UrlUserInfo $Url.Trim()
    git remote add gitlab $Url
    if ($LASTEXITCODE -ne 0) { Write-ErrorMsg 'git remote add 失败。'; exit 1 }
    Write-Success "已添加 remote gitlab -> $Url"
    $remoteUrl = $Url
  }

  # 3) 推送/拉取范围
  if ($Pull) {
    if ($All) { Write-ErrorMsg '-Pull 与 -All 互斥，二选一。'; exit 1 }
    if ($Tags) { Write-Warn '-Pull 模式忽略 -Tags。'; $Tags = $false }
    if ($Force) { Write-Warn '-Pull 模式忽略 -Force。'; $Force = $false }
  }
  if ($All -and -not [string]::IsNullOrWhiteSpace($Branch)) {
    Write-ErrorMsg '-All 与 -Branch 互斥，二选一。'
    exit 1
  }
  if (-not $All) {
    if ([string]::IsNullOrWhiteSpace($Branch)) {
      $Branch = git symbolic-ref --short -q HEAD
    }
    if ([string]::IsNullOrWhiteSpace($Branch)) {
      Write-ErrorMsg 'HEAD 处于分离状态：请用 -Branch 指定分支。'
      exit 1
    }
    $Branch = "$Branch".Trim()
  }

  # 4) 认证方式（显式 or Git 凭据管理器）
  $envUser = [string]$env:GITLAB_USERNAME
  $envPass = [string]$env:GITLAB_PASSWORD
  $useAuth = -not [string]::IsNullOrWhiteSpace($Username) -or
             -not [string]::IsNullOrWhiteSpace($envUser) -or
             -not [string]::IsNullOrWhiteSpace($envPass)
  $authMode = 'Git 凭据管理器（首次可能弹窗输入，可保存）'
  if ($useAuth) { $authMode = '显式认证（仅本次运行，不落盘）' }

  $scope = '全部分支'
  if (-not $All) { $scope = $Branch }
  if ($Tags) { $scope = "$scope + 全部标签" }

  if ($DryRun) {
    $mode = '推送（push）'
    if ($Pull) { $mode = '拉取合并（fetch + merge）' }
    Write-Info "[DryRun] 模式     : $mode"
    Write-Info "[DryRun] 目标     : gitlab -> $remoteUrl"
    Write-Info "[DryRun] 范围     : $scope"
    if (-not $Pull) {
      Write-Info "[DryRun] 强制覆盖 : $(if ($Force) { '是（先 fetch 再 --force-with-lease）' } else { '否' })"
    }
    Write-Info "[DryRun] 认证方式 : $authMode"
    Write-Info '[DryRun] 未执行任何变更。'
    exit 0
  }

  # 5) 显式认证：解析用户名/密码
  $authArgs = @()
  if ($useAuth) {
    $user = $Username
    if ([string]::IsNullOrWhiteSpace($user)) { $user = $envUser }
    if ([string]::IsNullOrWhiteSpace($user)) {
      $user = git config --local --get gitlab.username
      if ($LASTEXITCODE -ne 0) { $user = '' }
    }
    if ([string]::IsNullOrWhiteSpace($user)) {
      $user = Read-Host 'GitLab 用户名'
      if ([string]::IsNullOrWhiteSpace($user)) { Write-ErrorMsg '未输入用户名，已取消。'; exit 1 }
      $user = "$user".Trim()
      git config --local gitlab.username $user
      if ($LASTEXITCODE -ne 0) { Write-Warn '保存用户名到 git config 失败（下次可用 -Username 指定）。' }
    }
    $pass = $envPass
    if ([string]::IsNullOrWhiteSpace($pass)) { $pass = Get-PlainPassword }
    if ([string]::IsNullOrWhiteSpace($pass)) { Write-ErrorMsg '未提供密码/令牌，已取消。'; exit 1 }
    $basic = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("${user}:${pass}"))
    $authArgs = @('-c', 'credential.helper=', '-c', "http.extraheader=Authorization: Basic $basic")
    $pass = ''
  }

  # 6) 拉取合并模式：fetch + merge 后即结束（推回请再执行一次同步）
  if ($Pull) {
    $dirty = git status --porcelain
    if ($dirty) {
      Write-ErrorMsg '工作区有未提交改动：请先 commit 或 stash 后再拉取合并。'
      exit 1
    }
    Write-Info '拉取 gitlab 远端状态...'
    git @authArgs fetch gitlab
    if ($LASTEXITCODE -ne 0) { Write-ErrorMsg 'git fetch gitlab 失败（网络或认证问题）。'; exit 1 }
    $remoteRef = "gitlab/$Branch"
    git rev-parse --verify -q "$remoteRef" | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-ErrorMsg "远端分支不存在: $remoteRef"; exit 1 }
    $counts = @((git rev-list --left-right --count "HEAD...$remoteRef") -split '\s+' | Where-Object { $_ })
    $behind = [int]$counts[1]
    if ($behind -eq 0) {
      Write-Success "GitLab 侧无新提交，$Branch 已是最新。"
      exit 0
    }
    Write-Info "合并 $remoteRef（本地落后 $behind 个提交）..."
    git merge --no-edit $remoteRef
    if ($LASTEXITCODE -ne 0) {
      $gitDir = git rev-parse --absolute-git-dir
      if (Test-Path (Join-Path "$gitDir" 'MERGE_HEAD')) {
        Write-ErrorMsg '合并存在冲突：请解决冲突后 git add <文件> && git commit 完成合并；或 git merge --abort 放弃。'
      } else {
        Write-ErrorMsg "合并失败（git 退出码 $LASTEXITCODE）。"
      }
      exit 1
    }
    Write-Success "已合并 $remoteRef -> $Branch"
    Write-Info '如需推回 GitLab：再执行一次本脚本（不带 -Pull）。'
    exit 0
  }

  # 7) 强制覆盖前先同步远端状态（--force-with-lease 依赖 remote-tracking）
  if ($Force) {
    Write-Info '拉取 gitlab 远端状态（--force-with-lease 前置）...'
    git @authArgs fetch gitlab
    if ($LASTEXITCODE -ne 0) { Write-ErrorMsg 'git fetch gitlab 失败（网络或认证问题）。'; exit 1 }
  }

  # 8) 推送
  $pushArgs = @('push')
  if ($Force) { $pushArgs += '--force-with-lease' }
  if ($All) { $pushArgs += '--all' }
  if ($Tags) { $pushArgs += '--tags' }
  $pushArgs += 'gitlab'
  if (-not $All) { $pushArgs += $Branch }

  Write-Info "推送 $scope 到 gitlab..."
  # 捕获 git 输出（含 stderr）用于失败诊断；PS5.1 下 stderr 重定向需临时降 EAP
  $prevEAP = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  $pushOut = @(git @authArgs @pushArgs 2>&1)
  $pushExit = $LASTEXITCODE
  $ErrorActionPreference = $prevEAP
  $pushText = ($pushOut | ForEach-Object {
      if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.Exception.Message } else { "$_" }
    }) -join "`n"
  foreach ($line in ($pushText -split "`n")) { if ($line) { Write-Host $line } }

  if ($pushExit -ne 0) {
    Write-ErrorMsg "推送失败（git 退出码 $pushExit）。"
    $settingsHint = ''
    $groupHint = ''
    if ($remoteUrl -match '^https?://.+\.git$') { $settingsHint = ($remoteUrl -replace '\.git$', '') + '/-/settings/repository' }
    if ($remoteUrl -match '^https?://[^/]+/.+/.+\.git$') { $groupHint = (($remoteUrl -replace '\.git$', '') -replace '/[^/]+$', '') + '/-/settings/repository' }
    if ($pushText -match 'pre-receive hook declined' -or $pushText -match '(?i)protected') {
      Write-Warn '受保护分支被拒（GitLab protected branch）：空仓库的默认分支受保护、推送账号角色不足（与分支名无关，换 main/master 都会拒）。'
      Write-Warn '  方案1: 先确认推送用的是本人账号：凭据管理器可能缓存了其他账号（可删掉 git https://<host> 条目重新弹窗登录）。'
      Write-Warn '  方案2: 打开 Members 页核对角色；Developer 需 Maintainer/Owner 提升角色，或由其放开保护。'
      Write-Warn '  方案3: 有 Maintainer 权限时，在 Protected branches 将默认分支 Allowed to push 设为 "Developers and Maintainers"。'
      Write-Warn '  方案4: 请 Maintainer/Owner 推送初始提交（仅解除本次，后续推送仍需方案2/3）。'
      if ($settingsHint) { Write-Warn "  项目设置: $settingsHint" }
      if ($groupHint) { Write-Warn "  组级设置（项目设置被锁定或无权限时）: $groupHint" }
    }
    if ($pushText -match 'non-fast-forward|fetch first') {
      Write-Warn '若是非快进/分叉（远端初始化过 README、或远端有本地之外的提交）：加 -Force 覆盖。'
    }
    if ($pushText -match '(?i)Authentication failed|could not read Username|returned error: 40[13]|Invalid username or password|access denied') {
      Write-Warn '认证失败：设置 GITLAB_USERNAME / GITLAB_PASSWORD，或让凭据管理器保存；账号开启 2FA 时密码改用 Personal Access Token（scope: write_repository）。'
    }
    if ($pushText -match '(?i)SSL|certificate') {
      Write-Warn '证书错误：检查 gitlab.lunz.cn 证书链；临时可用 git config --local http.https://gitlab.lunz.cn.sslVerify false（不建议长期关闭）。'
    }
    exit 1
  }
  Write-Success "同步完成：$scope -> gitlab（$remoteUrl）"
}
catch {
  Write-ErrorMsg "脚本执行失败: $($_.Exception.Message)"
  exit 1
}
