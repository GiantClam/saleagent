# 前端启动脚本 (PowerShell)
# 使用方法: .\scripts\start-frontend.ps1

$ErrorActionPreference = "Stop"

# 获取脚本所在目录的父目录（项目根目录）
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
$WebDir = Join-Path $ProjectRoot "apps\web"

Write-Host "🚀 启动前端服务..." -ForegroundColor Green
Write-Host "📁 项目目录: $ProjectRoot" -ForegroundColor Cyan
Write-Host "📁 前端目录: $WebDir" -ForegroundColor Cyan

# 检查是否在正确的目录
$PackageJsonPath = Join-Path $WebDir "package.json"
if (-not (Test-Path $PackageJsonPath)) {
    Write-Host "❌ 错误: 找不到 apps/web/package.json" -ForegroundColor Red
    exit 1
}

# 进入前端目录
Set-Location $WebDir

# 检查 node_modules
$NodeModulesPath = Join-Path $WebDir "node_modules"
if (-not (Test-Path $NodeModulesPath)) {
    Write-Host "📥 安装依赖..." -ForegroundColor Yellow
    
    # 检测包管理器
    if (Get-Command pnpm -ErrorAction SilentlyContinue) {
        Write-Host "使用 pnpm..." -ForegroundColor Cyan
        pnpm install
    } elseif (Get-Command yarn -ErrorAction SilentlyContinue) {
        Write-Host "使用 yarn..." -ForegroundColor Cyan
        yarn install
    } else {
        Write-Host "使用 npm..." -ForegroundColor Cyan
        npm install
    }
} else {
    Write-Host "✅ 依赖已安装" -ForegroundColor Green
}

# 检查环境变量文件
$EnvLocalPath = Join-Path $WebDir ".env.local"
if (-not (Test-Path $EnvLocalPath)) {
    Write-Host "⚠️  警告: 找不到 .env.local 文件" -ForegroundColor Yellow
    Write-Host "💡 提示: 请创建 apps/web/.env.local 文件并配置环境变量" -ForegroundColor Cyan
    $EnvExamplePath = Join-Path $WebDir ".env.local.example"
    if (Test-Path $EnvExamplePath) {
        Write-Host "💡 参考: apps/web/.env.local.example" -ForegroundColor Cyan
    }
}

# 启动服务
Write-Host ""
Write-Host "🌟 启动 Next.js 开发服务器..." -ForegroundColor Green
Write-Host "📍 服务地址: http://localhost:3000" -ForegroundColor Cyan
Write-Host ""
Write-Host "按 Ctrl+C 停止服务" -ForegroundColor Yellow
Write-Host ""

# 检测包管理器并启动
if (Get-Command pnpm -ErrorAction SilentlyContinue) {
    pnpm dev
} elseif (Get-Command yarn -ErrorAction SilentlyContinue) {
    yarn dev
} else {
    npm run dev
}

