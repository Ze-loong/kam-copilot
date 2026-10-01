<#
一键启动脚本 —— 按 03-运维手册.md 记录的启动顺序，依次检查/拉起：
  1. Docker 容器（kam-postgres / kam-postgres-vector）
  2. kam_agent（8000，必须最先起）
  3. kam_admin（5001）+ kam_sidebar（8002）（互不依赖，谁先起都行）

每个服务开一个独立的新 PowerShell 窗口，方便单独看日志、单独 Ctrl+C 停止，
不会互相拖累。启动前会先检查目标端口是否已被占用，占用就跳过、不重复启动
（避免 WinError 10048 "端口已被占用"）。

脚本本身不注入任何密钥——DEEPSEEK_API_KEY 等敏感变量都在各组件自己的
.env 里，由各服务自己 load_dotenv() 读取，脚本只负责切目录+起进程。

用法：在本文件所在目录（项目根目录）执行 .\一键启动.ps1
#>

$root = $PSScriptRoot

function Get-PortOwnerPid {
    param([int]$Port)
    $result = netstat -ano | Select-String "LISTENING" | Select-String ":$Port\s"
    if ($result) {
        $fields = ($result.Line -split '\s+') | Where-Object { $_ -ne '' }
        return $fields[-1]
    }
    return $null
}

function Test-ServiceHealthy {
    <#
    只测"端口有人监听"测不出的一类问题：进程还活着、端口也在监听，但持有的
    资源已经失效—— 真实踩过一次，电脑/Docker重启后 Postgres
    容器换了一个全新的服务进程，但 kam_agent 这个 Windows 原生进程没有跟着
    重启，只看端口会误判它"正常在跑"，实际上一碰数据库就 500。这里发一次
    真实的业务请求验证，而不是只探测端口。
    #>
    param(
        [string]$Url,
        [string]$Body
    )
    try {
        if ($Body) {
            $resp = Invoke-WebRequest -Uri $Url -Method Post -ContentType "application/json" `
                -Body $Body -TimeoutSec 5 -UseBasicParsing
        } else {
            $resp = Invoke-WebRequest -Uri $Url -Method Get -TimeoutSec 5 -UseBasicParsing
        }
        return $resp.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Start-ServiceWindow {
    param(
        [string]$Title,
        [string]$WorkDir,
        [string]$Command,
        [int]$Port,
        [string]$HealthCheckUrl = $null,
        [string]$HealthCheckBody = $null
    )

    $existingPid = Get-PortOwnerPid -Port $Port
    if ($existingPid) {
        $procName = (Get-Process -Id $existingPid -ErrorAction SilentlyContinue).ProcessName

        if ($HealthCheckUrl) {
            Write-Host "[检测] $Title：端口 $Port 已被占用（PID $existingPid / $procName），探测是否真的健康..." -ForegroundColor DarkGray
            if (Test-ServiceHealthy -Url $HealthCheckUrl -Body $HealthCheckBody) {
                Write-Host "[跳过] $Title：探测正常，确实在正常运行，不重复启动。" -ForegroundColor Yellow
                return
            } else {
                $ownerCommand = (Get-CimInstance Win32_Process -Filter "ProcessId = $existingPid" -ErrorAction SilentlyContinue).CommandLine
                if ($ownerCommand -notlike "*$WorkDir*") {
                    throw "端口 $Port 被其他程序占用（PID $existingPid）。请先释放端口，不会自动结束该进程。"
                }
                Write-Host "[重启] $Title：端口被占用但探测失败（大概率是电脑/Docker重启后残留的僵尸进程，持有的数据库连接已失效），杀掉 PID $existingPid 后重新拉起..." -ForegroundColor Red
                Stop-Process -Id $existingPid -Force -ErrorAction SilentlyContinue
                Start-Sleep -Seconds 1
            }
        } else {
            Write-Host "[跳过] $Title：端口 $Port 已被占用（PID $existingPid / $procName），大概率已经在运行，不重复启动。" -ForegroundColor Yellow
            return
        }
    }

    Write-Host "[启动] $Title（端口 $Port）..." -ForegroundColor Cyan
    $titleCmd = "`$host.UI.RawUI.WindowTitle = '$Title'"
    $fullCommand = "$titleCmd; Set-Location -LiteralPath '$WorkDir'; $Command"
    Start-Process powershell -WindowStyle Hidden -ArgumentList "-NoExit", "-Command", $fullCommand | Out-Null
}

# ---------------------------------------------------------------------------
# 1. Docker 容器
# ---------------------------------------------------------------------------
Write-Host "=== 检查 Docker 容器 ===" -ForegroundColor Green

$dockerOk = $true
try {
    docker info *> $null
    if ($LASTEXITCODE -ne 0) { $dockerOk = $false }
} catch {
    $dockerOk = $false
}

if (-not $dockerOk) { throw "Docker Desktop 未就绪，请启动后重试。" }
$legacy = @(docker ps --format '{{.Names}}')
if (-not $env:KAM_DB_PORT -and -not $env:KAM_VECTOR_DB_PORT -and
    ($legacy -contains 'kam-postgres') -and ($legacy -contains 'kam-postgres-vector')) {
    Write-Host '[OK] 已有独立演示数据库在运行，继续使用 5442/5443。' -ForegroundColor Yellow
} else {
    docker compose --project-directory $root up -d
    if ($LASTEXITCODE -ne 0) { throw "docker compose up -d 失败，请检查端口和 Docker 日志。" }
}

# ---------------------------------------------------------------------------
# 2. kam_agent（必须最先起，kam_admin/kam_sidebar 依赖它）
# ---------------------------------------------------------------------------
Write-Host "`n=== 启动 kam_agent ===" -ForegroundColor Green
Start-ServiceWindow -Title "kam_agent (8000)" -WorkDir "$root\kam_agent" `
    -Command "uv run uvicorn src.webapp.webapp:app --port 8000" -Port 8000 `
    -HealthCheckUrl "http://127.0.0.1:8000/health"

Write-Host "等待 kam_agent /health（最多 60 秒）..." -ForegroundColor DarkGray
$deadline = (Get-Date).AddSeconds(60)
while (-not (Test-ServiceHealthy -Url "http://127.0.0.1:8000/health")) {
    if ((Get-Date) -ge $deadline) { throw "kam_agent 在 60 秒内未就绪，请查看 kam_agent 日志和数据库连接配置。" }
    Start-Sleep -Seconds 1
}

# ---------------------------------------------------------------------------
# 3. kam_admin / kam_sidebar（互不依赖，谁先起都行）
# ---------------------------------------------------------------------------
Write-Host "`n=== 启动 kam_admin / kam_sidebar ===" -ForegroundColor Green
Start-ServiceWindow -Title "kam_admin (5001)" -WorkDir "$root\kam_admin" `
    -Command "uv run python kam_admin_app.py" -Port 5001
Start-ServiceWindow -Title "kam_sidebar (8002)" -WorkDir "$root\kam_sidebar" `
    -Command "uv run python main.py" -Port 8002

Write-Host "`n=== 完成 ===" -ForegroundColor Green
Write-Host "kam_agent   http://127.0.0.1:8000/docs"
Write-Host "kam_admin   http://127.0.0.1:5001/login"
Write-Host "kam_sidebar http://127.0.0.1:8002/"
Write-Host "各服务开在独立窗口，标题栏能看出是哪个；单独关掉窗口/Ctrl+C 即可停止对应服务。"
