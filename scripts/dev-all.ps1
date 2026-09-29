# 一键启动本地测试环境（CRM + AI 前后端）
# 用法:
#   .\scripts\dev-all.ps1 start    # 启动全部 4 个服务
#   .\scripts\dev-all.ps1 status   # 查看状态
#   .\scripts\dev-all.ps1 stop     # 停止全部
#
# 服务清单:
#   1. AI Python 后端   :8310  sales-crm-ai
#   2. CRM Java 后端    :8080  sales-crm-api-service
#   3. AI Svelte 前端   :5173  sales-crm-ai/frontend
#   4. CRM Vue2 前端    :81    sales-crm-admin-ui

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('start', 'status', 'stop')]
    [string]$Action = 'start',
    [switch]$NoCrmUi,
    [switch]$NoAiUi,
    [switch]$NoCrmApi
)

$ErrorActionPreference = 'Continue'

# ============ 路径配置 ============
$AiRoot      = 'D:\github\CRM\sales-crm-ai'
$CrmApiRoot  = 'D:\github\CRM\sales-crm-api-service'
$CrmUiRoot   = 'D:\github\CRM\sales-crm-admin-ui'
$PidDir      = Join-Path $AiRoot '.local\pids'
$LogDir      = Join-Path $AiRoot '.local\logs'

# ============ 服务定义 ============
$Services = @(
    @{
        Name = 'ai-api';  Label = 'AI Python 后端'
        Port = 8310;  HealthUrl = 'http://127.0.0.1:8310/health'
        WorkDir = $AiRoot
        FilePath = (Join-Path $AiRoot '.venv\Scripts\python.exe')
        Args = @('scripts\serve.py', '--port', '8310')
        Enabled = $true
    },
    @{
        Name = 'crm-api';  Label = 'CRM Java 后端'
        Port = 8080;  HealthUrl = 'http://127.0.0.1:8080/actuator/health'
        WorkDir = (Join-Path $CrmApiRoot 'salescrm-web')
        FilePath = 'cmd'
        Args = @('/c', 'mvn spring-boot:run "-Dspring-boot.run.profiles=dev"')
        Enabled = (-not $NoCrmApi)
    },
    @{
        Name = 'ai-ui';  Label = 'AI Svelte 前端'
        Port = 5173;  HealthUrl = 'http://[::1]:5173'
        WorkDir = (Join-Path $AiRoot 'frontend')
        FilePath = 'cmd'
        Args = @('/c', 'npm run dev')
        Enabled = (-not $NoAiUi)
    },
    @{
        Name = 'crm-ui';  Label = 'CRM Vue2 前端'
        Port = 81;  HealthUrl = 'http://127.0.0.1:81'
        WorkDir = $CrmUiRoot
        FilePath = 'cmd'
        Args = @('/c', 'set NODE_OPTIONS=--openssl-legacy-provider && npm run serve')
        Enabled = (-not $NoCrmUi)
    }
)

# ============ 工具函数 ============
function New-WorkDirs {
    if (-not (Test-Path $PidDir)) { New-Item -Path $PidDir -ItemType Directory -Force | Out-Null }
    if (-not (Test-Path $LogDir)) { New-Item -Path $LogDir -ItemType Directory -Force | Out-Null }
}

function Save-Pid($Name, $Proc) {
    @{
        pid = $Proc.Id; name = $Name; started = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
    } | ConvertTo-Json | Set-Content (Join-Path $PidDir "$Name.json") -Encoding UTF8
}

function Get-SavedPid($Name) {
    $f = Join-Path $PidDir "$Name.json"
    if (Test-Path $f) {
        try { return (Get-Content $f -Raw | ConvertFrom-Json) } catch { return $null }
    }
    return $null
}

function Remove-PidFile($Name) {
    $f = Join-Path $PidDir "$Name.json"
    if (Test-Path $f) { Remove-Item $f -Force }
}

function Test-PortListening([int]$Port) {
    $conn = netstat -ano | Select-String ":$Port\s.*LISTENING"
    return [bool]$conn
}

function Test-HttpOk($Url, [int]$TimeoutSec = 3) {
    try {
        Invoke-WebRequest -Uri $Url -TimeoutSec $TimeoutSec -UseBasicParsing -ErrorAction Stop | Out-Null
        return $true
    } catch {
        # 401/403 也说明服务在响应
        if ($_.Exception.Response) { return $true }
        return $false
    }
}

function Test-ServiceReady($Svc) {
    if (Test-PortListening $Svc.Port) {
        if (Test-HttpOk $Svc.HealthUrl) { return 'running' }
        return 'port-only'
    }
    return 'stopped'
}

function Stop-ServiceByName($Name) {
    $record = Get-SavedPid $Name
    if ($record) {
        $proc = Get-Process -Id $record.pid -ErrorAction SilentlyContinue
        if ($proc) {
            try {
                # 杀进程树
                taskkill /PID $record.pid /T /F 2>$null | Out-Null
                Write-Host "  [stop] $Name (PID $($record.pid))" -ForegroundColor Yellow
            } catch {
                Write-Host "  [fail] $Name 停止失败: $_" -ForegroundColor Red
            }
        }
        Remove-PidFile $Name
    }
    # 兜底: 按端口找残留
    $conn = netstat -ano | Select-String ":$($Services | Where-Object { $_.Name -eq $Name } | Select-Object -ExpandProperty Port)\s.*LISTENING"
    if ($conn) {
        $pidToKill = ($conn -split '\s+')[-1]
        if ($pidToKill -match '^\d+$') {
            taskkill /PID $pidToKill /T /F 2>$null | Out-Null
            Write-Host "  [stop] $Name 端口残留 (PID $pidToKill)" -ForegroundColor Yellow
        }
    }
}

function Start-Service($Svc) {
    Write-Host "  启动 $($Svc.Label) (:$($Svc.Port)) ..." -ForegroundColor Cyan
    $outLog = Join-Path $LogDir "$($Svc.Name)-out.log"
    $errLog = Join-Path $LogDir "$($Svc.Name)-err.log"
    Remove-Item $outLog, $errLog -ErrorAction SilentlyContinue

    $proc = Start-Process -FilePath $Svc.FilePath -ArgumentList $Svc.Args `
        -WorkingDirectory $Svc.WorkDir -WindowStyle Hidden `
        -RedirectStandardOutput $outLog -RedirectStandardError $errLog -PassThru
    Save-Pid $Svc.Name $proc
    Write-Host "    PID: $($proc.Id)" -ForegroundColor DarkGray
}

function Wait-ServiceReady($Svc, [int]$TimeoutSec = 90) {
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $TimeoutSec) {
        $state = Test-ServiceReady $Svc
        if ($state -eq 'running') { return $true }
        Start-Sleep -Seconds 3
    }
    return $false
}

# ============ 动作 ============
switch ($Action) {

    'start' {
        New-WorkDirs
        Write-Host ""
        Write-Host "======== 启动本地测试环境 ========" -ForegroundColor Green

        # 预检
        Write-Host ""
        Write-Host "[预检]" -ForegroundColor White
        $aiPy = Join-Path $AiRoot '.venv\Scripts\python.exe'
        if (-not (Test-Path $aiPy)) {
            Write-Host "  [x] AI 虚拟环境缺失: $aiPy" -ForegroundColor Red
            Write-Host "     先执行: cd $AiRoot && python -m venv .venv && .venv\Scripts\pip install -r requirements.lock" -ForegroundColor Yellow
            exit 1
        }
        Write-Host "  [ok] AI 虚拟环境" -ForegroundColor Green

        foreach ($svc in $Services) {
            if (-not $svc.Enabled) { continue }
            if (-not $svc.WorkDir -or -not (Test-Path $svc.WorkDir)) {
                Write-Host "  [x] $($svc.Label) 工作目录不存在: $($svc.WorkDir)" -ForegroundColor Red
                exit 1
            }
            $nodeModules = Join-Path $svc.WorkDir 'node_modules'
            if ($svc.Args -match 'npm run') {
                if (-not (Test-Path $nodeModules)) {
                    Write-Host "  [x] $($svc.Label) 缺少 node_modules，请先 npm install" -ForegroundColor Red
                    exit 1
                }
                Write-Host "  [ok] $($svc.Label) node_modules" -ForegroundColor Green
            }
        }

        # 逐个启动
        Write-Host ""
        Write-Host "[启动]" -ForegroundColor White
        foreach ($svc in $Services) {
            if (-not $svc.Enabled) {
                Write-Host "  [skip] $($svc.Label)" -ForegroundColor DarkGray
                continue
            }
            $state = Test-ServiceReady $svc
            if ($state -eq 'running') {
                Write-Host "  [skip] $($svc.Label) 已运行 (: $($svc.Port))" -ForegroundColor Yellow
                continue
            }
            if ($state -eq 'port-only') {
                Write-Host "  [warn] $($svc.Label) 端口 $($svc.Port) 被占用但未通过健康检查" -ForegroundColor Yellow
                continue
            }
            Start-Service $svc
        }

        # 等待就绪
        Write-Host ""
        Write-Host "[等待就绪]" -ForegroundColor White
        foreach ($svc in $Services) {
            if (-not $svc.Enabled) { continue }
            Write-Host "  等待 $($svc.Label) ..." -NoNewline
            $ok = Wait-ServiceReady $svc -TimeoutSec 90
            if ($ok) {
                Write-Host " OK" -ForegroundColor Green
            } else {
                Write-Host " 超时" -ForegroundColor Red
                $outLog = Join-Path $LogDir "$($svc.Name)-out.log"
                $errLog = Join-Path $LogDir "$($svc.Name)-err.log"
                if (Test-Path $outLog) {
                    Write-Host "  --- $($svc.Name)-out.log 最后 5 行 ---" -ForegroundColor DarkGray
                    Get-Content $outLog -Tail 5 | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }
                }
                if (Test-Path $errLog) {
                    Write-Host "  --- $($svc.Name)-err.log 最后 5 行 ---" -ForegroundColor DarkGray
                    Get-Content $errLog -Tail 5 | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }
                }
            }
        }

        # 汇总
        Write-Host ""
        Write-Host "[入口]" -ForegroundColor Green
        foreach ($svc in $Services) {
            if (-not $svc.Enabled) { continue }
            $state = Test-ServiceReady $svc
            $icon = if ($state -eq 'running') { '[OK]' } elseif ($state -eq 'port-only') { '[!!]' } else { '[XX]' }
            $color = if ($state -eq 'running') { 'Green' } elseif ($state -eq 'port-only') { 'Yellow' } else { 'Red' }
            $url = $svc.HealthUrl -replace '/actuator/health', '' -replace '/health', ''
            Write-Host "  $icon $($svc.Label)" -ForegroundColor $color
            Write-Host "      $url" -ForegroundColor Cyan
        }
        Write-Host ""
        Write-Host "  AI 联调前端:   http://localhost:5173" -ForegroundColor Cyan
        Write-Host "  CRM 业务前端:  http://localhost:81" -ForegroundColor Cyan
        Write-Host "  日志目录:      $LogDir" -ForegroundColor DarkGray
        Write-Host ""
        Write-Host "======== 完成 ========" -ForegroundColor Green
    }

    'status' {
        Write-Host ""
        Write-Host "======== 服务状态 ========" -ForegroundColor White
        foreach ($svc in $Services) {
            if (-not $svc.Enabled) { continue }
            $state = Test-ServiceReady $svc
            $record = Get-SavedPid $svc.Name
            $pidStr = if ($record) { "PID $($record.pid) from $($record.started)" } else { '(no record)' }
            switch ($state) {
                'running'   { Write-Host "  [OK] $($svc.Label)  :$($svc.Port)  $pidStr" -ForegroundColor Green }
                'port-only' { Write-Host "  [!!] $($svc.Label)  :$($svc.Port)  端口监听但健康检查失败  $pidStr" -ForegroundColor Yellow }
                'stopped'   { Write-Host "  [--] $($svc.Label)  :$($svc.Port)  未运行" -ForegroundColor DarkGray }
            }
        }
        Write-Host ""
    }

    'stop' {
        Write-Host ""
        Write-Host "======== 停止本地测试环境 ========" -ForegroundColor Yellow
        foreach ($svc in $Services) {
            Stop-ServiceByName $svc.Name
        }
        Write-Host ""
        Write-Host "======== 已停止 ========" -ForegroundColor Yellow
    }
}
