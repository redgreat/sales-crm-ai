# sales-crm-ai 一键开发启动脚本（PowerShell）
#
# 用法：
#   .\scripts\dev.ps1 start   # 启动 AI API/worker 与前端（-NoUi 跳过前端）
#   .\scripts\dev.ps1 status  # 查看状态（区分运行中/不健康）
#   .\scripts\dev.ps1 stop    # 仅停止本脚本启动且验证过 PID 归属的进程
#
# 配置：conf/config.yml（复制 conf/config.yml.example 填写；含密钥不入库）。
# 遵循需求第 8 节：后台隐藏窗口、预检依赖/端口、PID/启动时间归属、
# 日志落 .local 忽略目录；不自动安装全局依赖、不自动执行数据库迁移。
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("start", "status", "stop")]
    [string]$Action = "status",

    [string]$PythonExe = "",
    [string]$ProjectRoot = "",
    [string]$ConfigFile = "",
    [string]$FrontendRoot = "",
    [int]$ApiPort = 0,
    [int]$UiPort = 5174,
    [switch]$NoUi
)

$ErrorActionPreference = "Stop"

if (-not $ProjectRoot) { $ProjectRoot = Split-Path -Parent $PSScriptRoot }
if (-not $PythonExe) {
    $candidate = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    if (Test-Path $candidate) { $PythonExe = $candidate } else { $PythonExe = "python" }
}
if (-not $ConfigFile) { $ConfigFile = Join-Path $ProjectRoot "conf\config.yml" }
if (-not $FrontendRoot) { $FrontendRoot = Join-Path $ProjectRoot "frontend" }

# 运行时配置（.local 下，gitignore）
$RuntimeDir = Join-Path $ProjectRoot ".local"
$LogDir = Join-Path $RuntimeDir "logs"
$RuntimeFile = Join-Path $RuntimeDir "dev-processes.json"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Get-ConfigValue {
    param([string]$Key, [string]$Default = "")
    $value = & $PythonExe (Join-Path $ProjectRoot "scripts\print_config.py") --config $ConfigFile $Key 2>$null
    if ($LASTEXITCODE -eq 0 -and $value) { return "$value".Trim() }
    return $Default
}

if (-not $ApiPort) {
    $ApiPort = [int](Get-ConfigValue "api.port" "8310")
}

function Get-ProcessRecord {
    param([string]$Name)
    if (-not (Test-Path $RuntimeFile)) { return $null }
    try { $all = Get-Content $RuntimeFile -Raw | ConvertFrom-Json } catch { return $null }
    if ($all.PSObject.Properties[$Name]) { return $all.$Name }
    return $null
}

function Save-ProcessRecord {
    param([string]$Name, $Record)
    $all = @{}
    if (Test-Path $RuntimeFile) {
        try { $all = Get-Content $RuntimeFile -Raw | ConvertFrom-Json -AsHashtable } catch { $all = @{} }
    }
    $all[$Name] = $Record
    $all | ConvertTo-Json -Depth 5 | Set-Content $RuntimeFile -Encoding UTF8
}

function Test-OwnedProcess {
    param([string]$Name)
    $record = Get-ProcessRecord $Name
    if ($null -eq $record) { return $null }
    $proc = Get-Process -Id $record.pid -ErrorAction SilentlyContinue
    if ($null -eq $proc) { return $null }
    # 验证命令归属：必须是本脚本记录的启动命令行
    $cmdLine = (Get-CimInstance Win32_Process -Filter "ProcessId=$($record.pid)").CommandLine
    if ($cmdLine -and $cmdLine -like "*$($record.command_hint)*") {
        return @{ Record = $record; Process = $proc }
    }
    Write-Host "[warn] $Name 的 PID $($record.pid) 存在但命令行不匹配，不纳入管理"
    return $null
}

function Test-HttpOk {
    param([string]$Url, [int]$TimeoutSec = 3)
    try {
        $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec $TimeoutSec
        return $response.StatusCode -eq 200
    } catch { return $false }
}

function Test-PortFree {
    param([int]$Port)
    $listener = $null
    try {
        $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
        $listener.Start()
        return $true
    } catch { return $false } finally {
        if ($listener) { $listener.Stop() }
    }
}

function Write-Fail {
    param([string]$Message)
    Write-Host "[FAIL] $Message" -ForegroundColor Red
}

# ---------- 预检 ----------
function Invoke-Preflight {
    $problems = @()

    # Python 虚拟环境与锁定依赖
    $venvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path $venvPython)) {
        $problems += "未找到虚拟环境 $venvPython；请先执行: python -m venv .venv && .venv\Scripts\pip install -r requirements.lock"
    } else {
        $lockFile = Join-Path $ProjectRoot "requirements.lock"
        if (Test-Path $lockFile) {
            $missing = & $venvPython -c @"
import sys
from importlib.metadata import version, PackageNotFoundError
missing = []
for line in open(r'$lockFile', encoding='utf-8'):
    line = line.strip()
    if not line or line.startswith('#'): continue
    name = (line.split('==')[0] if '==' in line else line).split('[')[0]
    try: version(name)
    except PackageNotFoundError: missing.append(name)
print(','.join(missing))
"@
            if ($missing) { $problems += "锁定依赖缺失: $missing（.venv 内执行 pip install -r requirements.lock）" }
        }
    }

    # 前端依赖（仅启动前端时）
    if (-not $NoUi) {
        if (-not (Get-Command node -ErrorAction SilentlyContinue)) { $problems += "未找到 node；前端需要 Node.js" }
        if (-not (Get-Command npm -ErrorAction SilentlyContinue)) { $problems += "未找到 npm" }
        if (-not (Test-Path (Join-Path $FrontendRoot "package.json"))) { $problems += "前端目录缺失或未初始化: $FrontendRoot" }
    }

    # 端口
    if (-not (Test-PortFree $ApiPort)) {
        if (Test-HttpOk "http://127.0.0.1:$ApiPort/health") {
            $problems += "端口 $ApiPort 已被本服务占用（可能已启动，请先 status 检查）"
        } else {
            $problems += "端口 $ApiPort 被其他程序占用，请改 conf/config.yml 的 api.port 或释放端口"
        }
    }

    # 配置文件
    if (-not (Test-Path $ConfigFile)) {
        $problems += "缺少配置文件 $ConfigFile；请复制 conf\config.yml.example 为 conf\config.yml 并填写"
    }

    # PostgreSQL 迁移/checkpoint 状态（只检查，不自动改库）
    $dbUrl = Get-ConfigValue "database_url" ""
    if (-not $dbUrl) {
        $problems += "配置中未设置 database_url"
    } else {
        $env:SAI_CONFIG = $ConfigFile
        & $PythonExe -c @"
import asyncio, sys
sys.path.insert(0, r'$ProjectRoot')
from app.persistence import migrations
from app.persistence.checkpoints import check_checkpoint_schema
from app.persistence.pool import connect

async def main():
    try:
        async with connect(r'$dbUrl') as conn:
            ok, current = await migrations.check_schema_revision(conn)
        cp_ok, _ = await check_checkpoint_schema(r'$dbUrl')
    except Exception as exc:
        print(f'NOCONN:{exc}'); return
    if not ok: print(f'SCHEMA:{current}')
    if not cp_ok: print('CHECKPOINT:missing')

asyncio.run(main())
"@ | ForEach-Object {
            if ($_.StartsWith("NOCONN:")) { $problems += "数据库连接失败: $($_.Substring(7))" }
            elseif ($_.StartsWith("SCHEMA:")) { $problems += "业务表结构未就绪（当前 $($_.Substring(7))）；请执行 scripts\init_db.py --apply（显式授权）" }
            elseif ($_ -eq "CHECKPOINT:missing") { $problems += "checkpoint 结构未初始化；请执行 scripts\init_checkpoints.py --apply（显式授权）" }
        }
        Remove-Item Env:SAI_CONFIG -ErrorAction SilentlyContinue
    }

    return $problems
}

# ---------- 启动 ----------
function Start-ProcessHidden {
    param([string]$FilePath, [string[]]$Arguments, [string]$WorkingDir, [string]$LogPath)
    $logStream = [System.IO.File]::AppendText($LogPath)
    $logStream.WriteLine("==== start $($FilePath) $($Arguments -join ' ') at $(Get-Date -Format o) ====")
    $logStream.Close()
    $psi = [System.Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = $FilePath
    foreach ($arg in $Arguments) { [void]$psi.ArgumentList.Add($arg) }
    $psi.WorkingDirectory = $WorkingDir
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $process = [System.Diagnostics.Process]::Start($psi)
    # 输出异步落日志（日志中不含密钥；密钥只在 conf/config.yml）
    Start-Job -ScriptBlock {
        param($proc2, $outLog)
        while (-not $proc2.HasExited) {
            $line = $proc2.StandardOutput.ReadLine()
            if ($null -ne $line) { Add-Content -Path $outLog -Value $line }
            Start-Sleep -Milliseconds 50
        }
    } -ArgumentList $process, $LogPath | Out-Null
    return $process
}

function Invoke-Start {
    Write-Host "== sales-crm-ai dev 启动 ==" -ForegroundColor Cyan
    $existing = Test-OwnedProcess "ai-api"
    if ($existing) {
        Write-Host "[skip] AI API 已在运行（PID $($existing.Record.pid)），start 幂等"
    } else {
        $problems = Invoke-Preflight
        if ($problems) {
            Write-Fail "预检未通过："
            $problems | ForEach-Object { Write-Host "  - $_" -ForegroundColor Yellow }
            exit 1
        }

        $apiLog = Join-Path $LogDir "ai-api.log"
        $proc = Start-ProcessHidden -FilePath $PythonExe `
            -Arguments @("scripts\serve.py", "--port", "$ApiPort") `
            -WorkingDir $ProjectRoot -LogPath $apiLog
        Save-ProcessRecord "ai-api" @{
            pid = $proc.Id
            started_at = (Get-Date -Format o)
            command = "scripts\serve.py --port $ApiPort"
            command_hint = "scripts\serve.py"
            log = $apiLog
        }
        Write-Host "[ok] AI API 启动中（PID $($proc.Id)），日志: $apiLog"

        # 就绪等待（最多 30 秒）
        $ready = $false
        foreach ($i in 1..60) {
            if (Test-HttpOk "http://127.0.0.1:$ApiPort/health") { $ready = $true; break }
            if ($proc.HasExited) { break }
            Start-Sleep -Milliseconds 500
        }
        if (-not $ready) {
            Write-Fail "AI API 未在预期时间内就绪；本次启动进程将被清理"
            try { $proc.Kill() } catch {}
            exit 1
        }
        Write-Host "[ok] AI API 就绪: http://127.0.0.1:$ApiPort/health （/ready 为结构校验结果）"
    }

    if (-not $NoUi) {
        $uiExisting = Test-OwnedProcess "ai-ui"
        if ($uiExisting) {
            Write-Host "[skip] 前端已在运行（PID $($uiExisting.Record.pid)）"
        } else {
            if (Test-Path (Join-Path $FrontendRoot "package.json")) {
                $uiLog = Join-Path $LogDir "ai-ui.log"
                $procUi = Start-ProcessHidden -FilePath "cmd.exe" `
                    -Arguments @("/c", "npm run dev -- --port $UiPort --strictPort") `
                    -WorkingDir $FrontendRoot -LogPath $uiLog
                Save-ProcessRecord "ai-ui" @{
                    pid = $procUi.Id
                    started_at = (Get-Date -Format o)
                    command = "npm run dev -- --port $UiPort"
                    command_hint = "npm run dev"
                    log = $uiLog
                }
                Write-Host "[ok] 前端启动中（PID $($procUi.Id)），日志: $uiLog"
            } else {
                Write-Host "[skip] 前端目录未初始化（$FrontendRoot），跳过"
            }
        }
    }

    Write-Host ""
    Write-Host "入口：" -ForegroundColor Green
    Write-Host "  AI API:      http://127.0.0.1:$ApiPort"
    Write-Host "  AI 健康:     http://127.0.0.1:$ApiPort/health"
    Write-Host "  AI 就绪:     http://127.0.0.1:$ApiPort/ready"
    if (-not $NoUi) {
        Write-Host "  AI 联调前端: http://127.0.0.1:$UiPort （playground）"
    }
}

# ---------- 状态 ----------
function Invoke-Status {
    $healthOk = Test-HttpOk "http://127.0.0.1:$ApiPort/health"
    $record = Get-ProcessRecord "ai-api"
    if ($record -and $healthOk) {
        $readyOk = Test-HttpOk "http://127.0.0.1:$ApiPort/ready"
        $state = if ($readyOk) { "运行中（健康）" } else { "运行中（不健康：/ready 未通过，检查数据库结构与 checkpoint）" }
        Write-Host "ai-api: $state，PID $($record.pid)，启动于 $($record.started_at)"
    } elseif ($record) {
        Write-Host "ai-api: 已记录 PID $($record.pid) 但健康检查失败（不健康/已退出）"
    } elseif ($healthOk) {
        Write-Host "ai-api: 端口有健康响应但非本脚本启动（不纳入管理）"
    } else {
        Write-Host "ai-api: 未运行"
    }
    $ui = Test-OwnedProcess "ai-ui"
    if ($ui) { Write-Host "ai-ui: 运行中，PID $($ui.Record.pid)" } else { Write-Host "ai-ui: 未运行" }
}

# ---------- 停止 ----------
function Invoke-Stop {
    foreach ($name in @("ai-api", "ai-ui")) {
        $owned = Test-OwnedProcess $name
        if ($owned) {
            try {
                Stop-Process -Id $owned.Record.pid -Force -ErrorAction Stop
                Write-Host "[ok] 已停止 $name（PID $($owned.Record.pid)）"
            } catch { Write-Host "[warn] 停止 $name 失败: $_" }
        } else {
            Write-Host "[skip] $name 没有本脚本启动且验证过的进程"
        }
    }
    if (Test-Path $RuntimeFile) { Remove-Item $RuntimeFile -Force }
}

switch ($Action) {
    "start" { Invoke-Start }
    "status" { Invoke-Status }
    "stop" { Invoke-Stop }
}
