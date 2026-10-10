#!/usr/bin/env pwsh
<#
  sales-crm-ai 从私有 GitLab 拉取「已合入」的代码（sync-gitlab.ps1 的反向操作）。
  主仓库仍是 GitHub（origin）；本脚本只读/只操作名为 gitlab 的 remote，绝不改写 GitHub 历史。

  典型场景：GitLab 侧的 MR 已合入默认分支，要把这些提交取回本地，由人工审查/合并后推回 GitHub。

  用法:
    .\scripts\pull-gitlab.ps1                      # 拉取 GitLab 默认分支，落到本地 gitlab-<branch>，不动工作区
    .\scripts\pull-gitlab.ps1 -Branch main         # 指定 GitLab 分支（默认自动探测 main/master）
    .\scripts\pull-gitlab.ps1 -Merge               # 拉取后直接合并进当前分支（需工作区干净）
    .\scripts\pull-gitlab.ps1 -Push                # 合并后再推回 GitHub origin（仅快进，不 force）
    .\scripts\pull-gitlab.ps1 -Mrs                 # 同时抓取 MR refs（refs/merge-requests/*）
    .\scripts\pull-gitlab.ps1 -DryRun              # 只打印动作，不落任何变更

  与 GitHub 协作的约束（保证推得回去）:
    * 只做 fetch / merge，绝不 rebase、绝不 force-push origin —— 本地提交始终是 origin/main 的后代，
      合并后 `git push origin main` 就是普通快进，不需要 --force。
    * 若 GitLab 与 GitHub 历史没有共同祖先（GitLab 侧是独立初始化的仓库），脚本会明确告警并停止合并，
      此时不能直接合并——需要人工决定以哪边为准。

  首次若未配置 gitlab remote: .\scripts\pull-gitlab.ps1 -Url https://gitlab.lunz.cn/<组>/sales-crm-ai.git
#>

param(
  [string]$Url = '',
  [string]$Branch = '',
  [string]$LocalBranch = '',
  [string]$Username = '',
  [switch]$Merge,
  [switch]$Mrs,
  [switch]$Push,
  [switch]$DryRun,
  [Alias('h')]
  [switch]$Help
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Show-Usage {
  @'
用法: .\scripts\pull-gitlab.ps1 [选项]

拉取 GitLab 侧已合入的代码到本地，由人工合并后再推回 GitHub（origin）。
只 fetch / merge，不 rebase、不 force，保证 git push origin main 仍是快进。

选项:
  -Branch <string>     GitLab 分支（默认自动探测：main > master > ls-remote 默认分支）
  -LocalBranch <s>     落点本地分支（默认 gitlab-<Branch>；不会自动切换工作区）
  -Merge               拉取后立即合并进当前分支（需工作区干净，默认只落地不合并）
  -Push                合并后再推回 GitHub origin（默认不推；仅快进，拒绝 non-fast-forward）
  -Mrs                 同时抓取 GitLab MR refs 到 refs/remotes/gitlab/mr/*（评审用）
  -Url <string>        GitLab 仓库地址（remote 缺失时首次需要；保存为 gitlab remote）
  -Username <s>        GitLab 用户名（默认: GITLAB_USERNAME > git config gitlab.username > 提示输入）
  -DryRun              只打印将执行的动作，不添加 remote、不 fetch
  -Help                显示帮助

认证（二选一）:
  1) 默认: 走 Git 凭据管理器——首次弹窗输入后可保存，之后免输。
  2) 显式: 设置环境变量 GITLAB_USERNAME / GITLAB_PASSWORD，仅本次运行使用、不落盘。
     账号开启 2FA 时密码请改用 Personal Access Token（scope: read_repository 即可，只读拉取）。

说明: 主仓库仍为 GitHub；本脚本只操作 gitlab remote，不影响 origin。
'@ | Write-Host
}

if ($Help) { Show-Usage; exit 0 }

# 彩色日志函数（与 sync-gitlab.ps1 / dockerbuild.ps1 一致）
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

function Invoke-GitCapture([string[]]$GitArgs) {
  # 统一捕获 git 输出（含 stderr），PS5.1 下需临时降级 EAP
  $prevEAP = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try {
    $out = @(& git @GitArgs 2>&1)
    $code = $LASTEXITCODE
  }
  finally {
    $ErrorActionPreference = $prevEAP
  }
  $text = ($out | ForEach-Object {
      if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.Exception.Message } else { "$_" }
    }) -join "`n"
  return @{ Code = $code; Text = $text }
}

function Test-RefExists([string]$Ref) {
  git rev-parse --verify -q "$Ref" | Out-Null
  return ($LASTEXITCODE -eq 0)
}

function Get-GitlabRemoteUrl {
  # 返回已配置的 gitlab remote 地址（无则空串）
  if (@((git remote)) -contains 'gitlab') { return ("$(git remote get-url gitlab)").Trim() }
  return ''
}

try {
  # 1) 确认位于 Git 仓库
  $inside = git rev-parse --is-inside-work-tree
  if ($LASTEXITCODE -ne 0 -or "$inside".Trim() -ne 'true') {
    Write-ErrorMsg '当前目录不是 Git 仓库。'
    exit 1
  }
  if ($Merge -and $Push) {
    Write-Warn '-Merge 与 -Push 同时给出：将先合并再推回 GitHub。'
  }

  # 2) 解析 gitlab remote（与 sync-gitlab.ps1 共用同一 remote，首次需 -Url）
  $remoteUrl = Get-GitlabRemoteUrl
  if ($Url) {
    $Url = Remove-UrlUserInfo $Url.Trim()
    if (-not $remoteUrl) {
      if ($DryRun) {
        Write-Info "[DryRun] 将添加 remote 'gitlab' -> $Url"
      }
      else {
        git remote add gitlab $Url
        if ($LASTEXITCODE -ne 0) { Write-ErrorMsg 'git remote add 失败。'; exit 1 }
        Write-Success "已添加 remote gitlab -> $Url"
      }
      $remoteUrl = $Url
    }
    elseif ($remoteUrl -ne $Url) {
      if ($DryRun) {
        Write-Info "[DryRun] 将更新 remote 'gitlab': $remoteUrl -> $Url"
      }
      else {
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
    $Url = Read-Host '首次拉取：输入 GitLab 仓库地址（如 https://gitlab.lunz.cn/WIN/sales-crm-ai.git）'
    if ([string]::IsNullOrWhiteSpace($Url)) { Write-ErrorMsg '未输入地址，已取消。'; exit 1 }
    $Url = Remove-UrlUserInfo $Url.Trim()
    git remote add gitlab $Url
    if ($LASTEXITCODE -ne 0) { Write-ErrorMsg 'git remote add 失败。'; exit 1 }
    Write-Success "已添加 remote gitlab -> $Url"
    $remoteUrl = $Url
  }

  # 3) 认证方式（显式 or Git 凭据管理器；拉取本身多数仓库匿名可读，认证失败才需要）
  $envUser = [string]$env:GITLAB_USERNAME
  $envPass = [string]$env:GITLAB_PASSWORD
  $useAuth = -not [string]::IsNullOrWhiteSpace($Username) -or
             -not [string]::IsNullOrWhiteSpace($envUser) -or
             -not [string]::IsNullOrWhiteSpace($envPass)
  $authMode = 'Git 凭据管理器（首次可能弹窗输入，可保存）'
  if ($useAuth) { $authMode = '显式认证（仅本次运行，不落盘）' }

  if ($DryRun) {
    $mode = 'fetch'
    if ($Merge) { $mode = "$mode + merge" }
    if ($Push) { $mode = "$mode + push origin" }
    Write-Info "[DryRun] 模式     : 拉取 GitLab 已合入代码（$mode）"
    Write-Info "[DryRun] 目标     : gitlab -> $remoteUrl"
    Write-Info "[DryRun] 认证方式 : $authMode"
    Write-Info '[DryRun] 未执行任何变更。'
    exit 0
  }

  # 4) 显式认证：解析用户名/密码
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

  # 5) 合并/推送前必须工作区干净
  $needClean = $Merge -or $Push
  if ($needClean) {
    $dirty = git status --porcelain
    if ($dirty) {
      Write-ErrorMsg '工作区有未提交改动：请先 commit 或 stash 后再合并/推送。'
      exit 1
    }
  }

  # 6) 抓取 GitLab 远端分支（含 prune，清掉 GitLab 侧已删除的分支引用）
  Write-Info '拉取 gitlab 远端状态...'
  $fetch = Invoke-GitCapture @authArgs 'fetch' 'gitlab' '--prune' '--tags'
  if ($fetch.Code -ne 0) {
    Write-ErrorMsg 'git fetch gitlab 失败（网络或认证问题）：'
    Write-Host $fetch.Text
    exit 1
  }

  # 7) 解析分支：显式 > main > master > ls-remote 默认分支
  if ([string]::IsNullOrWhiteSpace($Branch)) {
    foreach ($cand in 'main', 'master') {
      if (Test-RefExists "refs/remotes/gitlab/$cand") { $Branch = $cand; break }
    }
  }
  if ([string]::IsNullOrWhiteSpace($Branch)) {
    $sym = Invoke-GitCapture @authArgs 'ls-remote' '--symref' 'gitlab' 'HEAD'
    if ($sym.Code -eq 0 -and $sym.Text -match 'refs/heads/(\S+)') { $Branch = $Matches[1] }
  }
  if ([string]::IsNullOrWhiteSpace($Branch)) {
    Write-ErrorMsg '未找到 GitLab 默认分支：请用 -Branch 指定。'
    exit 1
  }
  $Branch = "$Branch".Trim()
  $remoteRef = "gitlab/$Branch"
  if (-not (Test-RefExists "refs/remotes/$remoteRef")) {
    Write-ErrorMsg "远端分支不存在: $remoteRef"
    exit 1
  }

  # 8) 可选：抓取 MR refs（GitLab 的 refs/merge-requests/*，用于评审未合入/已合入的 MR）
  if ($Mrs) {
    Write-Info '抓取 GitLab MR refs...'
    $mr = Invoke-GitCapture @authArgs 'fetch' 'gitlab' '+refs/merge-requests/*/head:refs/remotes/gitlab/mr/*'
    if ($mr.Code -ne 0) {
      Write-Warn 'MR refs 抓取失败（多数是仓库未开放或网络问题），不影响主分支拉取：'
      Write-Host $mr.Text
    }
    else {
      Write-Success 'MR refs 已更新：git ls-remote gitlab "refs/merge-requests/*" 可查看。'
    }
  }

  # 9) 落到本地分支（只动 ref，不切换工作区，绝不影响你当前的改动）
  if ([string]::IsNullOrWhiteSpace($LocalBranch)) { $LocalBranch = "gitlab-$Branch" }
  $LocalBranch = "$LocalBranch".Trim()
  $currentBranch = "$(git branch --show-current)".Trim()
  if ($LocalBranch -eq $currentBranch) {
    Write-ErrorMsg "落点分支 '$LocalBranch' 正是当前分支：请用 -LocalBranch 指定别的名字（本地分支不能被强制更新）。"
    exit 1
  }
  git branch -f $LocalBranch "$remoteRef"
  if ($LASTEXITCODE -ne 0) { Write-ErrorMsg "创建/更新本地分支 $LocalBranch 失败。"; exit 1 }
  Write-Success "已落地本地分支 $LocalBranch -> $remoteRef"

  # 10) 与 GitHub（origin）对比：来了多少提交、改了哪些文件、是否能快进推回
  $originRef = "origin/$Branch"
  $hasOrigin = (@((git remote)) -contains 'origin') -and (Test-RefExists "refs/remotes/$originRef")
  $base = ''
  if ($hasOrigin) { $base = "$(git merge-base "refs/remotes/$originRef" "$remoteRef" 2>$null)".Trim() }

  if ($hasOrigin -and $base) {
    $incoming = @(git log --oneline "refs/remotes/$originRef..$remoteRef")
    $counts = @((git rev-list --left-right --count "refs/remotes/$originRef...$remoteRef") -split '\s+' | Where-Object { $_ })
    $behind = if ($counts.Count -ge 2) { [int]$counts[1] } else { 0 }
    $ahead = if ($counts.Count -ge 1) { [int]$counts[0] } else { 0 }
    if ($behind -eq 0) {
      Write-Success "GitLab 侧无新提交（$Branch 与 origin/$Branch 一致）。"
    }
    else {
      Write-Info "GitLab 侧领先 origin/$Branch：$behind 个新提交（本地领先 $ahead）"
      Write-Host ($incoming -join "`n")
      $stat = Invoke-GitCapture 'diff' '--stat' "$base" "$remoteRef"
      if ($stat.Text.Trim()) { Write-Host $stat.Text }
    }
  }
  elseif ($hasOrigin -and -not $base) {
    Write-ErrorMsg '检测到 GitLab 与 GitHub 历史没有共同祖先（GitLab 侧是独立初始化的仓库）。'
    Write-Warn  '  此时不能直接 merge：合并会产生无关历史，推回 GitHub 也不再是快进。'
    Write-Warn  '  处理办法（人工决策）：以一边为准重建 remote，或只在 GitLab 侧保留、GitHub 不再同步。'
    exit 1
  }
  else {
    Write-Warn '本地没有 origin 远端分支（未连接 GitHub）：拉取可用，推回 GitHub 前请先关联 origin。'
  }

  # 11) 可选：合并进当前分支
  if ($Merge) {
    Write-Info "合并 $remoteRef -> $currentBranch ..."
    $merge = Invoke-GitCapture 'merge' '--no-edit' "$remoteRef"
    if ($merge.Code -ne 0) {
      $gitDir = git rev-parse --absolute-git-dir
      if (Test-Path (Join-Path "$gitDir" 'MERGE_HEAD')) {
        Write-ErrorMsg '合并存在冲突：请解决冲突后 git add <文件> && git commit 完成合并；或 git merge --abort 放弃。'
      }
      else {
        Write-ErrorMsg "合并失败（git 退出码 $($merge.Code)）："
      }
      Write-Host $merge.Text
      exit 1
    }
    Write-Host $merge.Text
    Write-Success "已合并 $remoteRef -> $currentBranch"
  }

  # 12) GitHub 可推性校验：本地提交必须是 origin/<Branch> 的后代（快进）
  if ($Push -or $hasOrigin) {
    $head = "$(git rev-parse HEAD)".Trim()
    $originHead = if ($hasOrigin) { "$(git rev-parse "refs/remotes/$originRef")".Trim() } else { '' }
    if ($hasOrigin -and $originHead -and $originHead -ne $head) {
      git merge-base --is-ancestor "$originHead" HEAD
      if ($LASTEXITCODE -ne 0) {
        Write-Warn "HEAD 不是 origin/$Branch 的后代：之间出现分叉，git push origin $Branch 会被拒绝（non-fast-forward）。"
        Write-Warn '  先查清分叉来源（是否有人从别处推了 GitHub）：git log --oneline --graph --left-right HEAD...origin/'"$Branch"
        Write-Warn "  确认要保留本地历史时再人工合并 origin/$Branch（git merge origin/$Branch），不要 force。"
        if ($Push) { Write-ErrorMsg '已停止推回 GitHub（避免推送被拒/误覆盖）。'; exit 1 }
      }
    }
  }

  if ($Push) {
    if (-not $Merge) {
      Write-ErrorMsg '-Push 只用于合并之后推回 GitHub；未合并时请先 -Merge（或自行 git merge 后重跑 -Push）。'
      exit 1
    }
    Write-Info "推回 GitHub origin/$Branch ..."
    $push = Invoke-GitCapture 'push' 'origin' "$Branch"
    if ($push.Code -ne 0) {
      Write-ErrorMsg "git push origin $Branch 失败（git 退出码 $($push.Code)）："
      Write-Host $push.Text
      Write-Warn '若是 non-fast-forward：先确认本地与 origin 的分叉，按上面提示处理，不要用 --force。'
      exit 1
    }
    Write-Host $push.Text
    Write-Success "已推回 GitHub：origin/$Branch"
  }

  # 13) 收尾：给出手工合并/推回的命令
  $reviewRange = if ($base) { "$base..$LocalBranch" } else { "$LocalBranch" }
  Write-Info '后续人工处理：'
  Write-Host "  git log --oneline --stat $reviewRange      # 审查 GitLab 来的改动" -ForegroundColor DarkGray
  Write-Host "  git diff $LocalBranch                            # 与本地当前分支的差异" -ForegroundColor DarkGray
  Write-Host "  git merge --no-edit $LocalBranch                 # 满意后合并进当前分支" -ForegroundColor DarkGray
  Write-Host "  git push origin $Branch                                    # 推回 GitHub（快进）" -ForegroundColor DarkGray
  Write-Host "  .\scripts\sync-gitlab.ps1                                # 需要时再同步回 GitLab" -ForegroundColor DarkGray
  Write-Success "完成：GitLab 已合入代码已在本地分支 $LocalBranch，工作区未受影响。"
}
catch {
  Write-ErrorMsg "脚本执行失败: $($_.Exception.Message)"
  exit 1
}
