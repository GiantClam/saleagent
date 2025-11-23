# 后端启动脚本 (PowerShell)
# 使用方法: .\scripts\start-backend.ps1

$ErrorActionPreference = "Stop"

# 获取脚本所在目录的父目录（项目根目录）
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
$AgentDir = Join-Path $ProjectRoot "apps\agent"

Write-Host "🚀 启动后端服务..." -ForegroundColor Green
Write-Host "📁 项目目录: $ProjectRoot" -ForegroundColor Cyan
Write-Host "📁 后端目录: $AgentDir" -ForegroundColor Cyan

# 检查是否在正确的目录
$MainPyPath = Join-Path $AgentDir "main.py"
if (-not (Test-Path $MainPyPath)) {
    Write-Host "❌ 错误: 找不到 apps/agent/main.py" -ForegroundColor Red
    exit 1
}

# 进入后端目录
Set-Location $AgentDir

# 检查虚拟环境
$VenvPath = Join-Path $AgentDir "venv"
if (-not (Test-Path $VenvPath)) {
    Write-Host "📦 创建虚拟环境..." -ForegroundColor Yellow
    python -m venv venv
}

# 激活虚拟环境
Write-Host "🔧 激活虚拟环境..." -ForegroundColor Yellow
& "$VenvPath\Scripts\Activate.ps1"

# 检查并安装依赖
$InstalledFlag = Join-Path $VenvPath ".installed"
if (-not (Test-Path $InstalledFlag)) {
    Write-Host "📥 安装依赖..." -ForegroundColor Yellow
    python -m pip install --upgrade pip
    
    # 使用分步安装脚本避免依赖解析问题
    $InstallDepsScript = Join-Path $AgentDir "install-deps.sh"
    if (Test-Path $InstallDepsScript) {
        Write-Host "使用 install-deps.sh 安装依赖..." -ForegroundColor Cyan
        # 在 Windows 上，如果有 WSL 或 Git Bash，可以尝试使用
        # 否则直接使用 pip install
        pip install -r requirements.txt
    } else {
        pip install -r requirements.txt
    }
    
    New-Item -ItemType File -Path $InstalledFlag -Force | Out-Null
} else {
    Write-Host "✅ 依赖已安装" -ForegroundColor Green
    
    # 验证关键依赖是否存在
    $SupabaseCheck = python -c "import supabase" 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "⚠️  检测到依赖缺失，重新安装..." -ForegroundColor Yellow
        pip install -r requirements.txt
    }
    
    # 验证 nest-asyncio 是否存在
    $NestAsyncioCheck = python -c "import nest_asyncio" 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "⚠️  检测到 nest-asyncio 缺失，安装中..." -ForegroundColor Yellow
        pip install nest-asyncio==1.6.0
    }
}

# 检查环境变量文件
$EnvPath = Join-Path $AgentDir ".env"
if (-not (Test-Path $EnvPath)) {
    Write-Host "⚠️  警告: 找不到 .env 文件" -ForegroundColor Yellow
    Write-Host "💡 提示: 请创建 apps/agent/.env 文件并配置环境变量" -ForegroundColor Cyan
    $EnvExamplePath = Join-Path $AgentDir ".env.example"
    if (Test-Path $EnvExamplePath) {
        Write-Host "💡 参考: apps/agent/.env.example" -ForegroundColor Cyan
    }
}

# 启动服务
Write-Host ""
Write-Host "🌟 启动 FastAPI 服务..." -ForegroundColor Green
Write-Host "📍 服务地址: http://localhost:8000" -ForegroundColor Cyan
Write-Host "📚 API 文档: http://localhost:8000/docs" -ForegroundColor Cyan
Write-Host ""
Write-Host "按 Ctrl+C 停止服务" -ForegroundColor Yellow
Write-Host ""

# 使用虚拟环境中的 python 运行 uvicorn
python -m uvicorn main:app --reload --port 8000

