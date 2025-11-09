#!/bin/bash

# 前端启动脚本
# 使用方法: ./scripts/start-frontend.sh

set -e

# 获取脚本所在目录的父目录（项目根目录）
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"
WEB_DIR="$PROJECT_ROOT/apps/web"

echo "🚀 启动前端服务..."
echo "📁 项目目录: $PROJECT_ROOT"
echo "📁 前端目录: $WEB_DIR"

# 检查是否在正确的目录
if [ ! -f "$WEB_DIR/package.json" ]; then
    echo "❌ 错误: 找不到 apps/web/package.json"
    exit 1
fi

# 进入前端目录
cd "$WEB_DIR"

# 检查 node_modules
if [ ! -d "node_modules" ]; then
    echo "📥 安装依赖..."
    if command -v pnpm &> /dev/null; then
        echo "使用 pnpm..."
        pnpm install
    elif command -v yarn &> /dev/null; then
        echo "使用 yarn..."
        yarn install
    else
        echo "使用 npm..."
        npm install
    fi
else
    echo "✅ 依赖已安装"
fi

# 检查环境变量文件
if [ ! -f ".env.local" ]; then
    echo "⚠️  警告: 找不到 .env.local 文件"
    echo "💡 提示: 请创建 apps/web/.env.local 文件并配置环境变量"
    echo "💡 参考: apps/web/.env.local.example (如果存在)"
fi

# 启动服务
echo "🌟 启动 Next.js 开发服务器..."
echo "📍 服务地址: http://localhost:3000"
echo ""
echo "按 Ctrl+C 停止服务"
echo ""

if command -v pnpm &> /dev/null; then
    pnpm dev
elif command -v yarn &> /dev/null; then
    yarn dev
else
    npm run dev
fi

