#!/bin/bash

# 后端启动脚本
# 使用方法: ./scripts/start-backend.sh

set -e

# 获取脚本所在目录的父目录（项目根目录）
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"
AGENT_DIR="$PROJECT_ROOT/apps/agent"

echo "🚀 启动后端服务..."
echo "📁 项目目录: $PROJECT_ROOT"
echo "📁 后端目录: $AGENT_DIR"

# 检查是否在正确的目录
if [ ! -f "$AGENT_DIR/main.py" ]; then
    echo "❌ 错误: 找不到 apps/agent/main.py"
    exit 1
fi

# 进入后端目录
cd "$AGENT_DIR"

# 检查虚拟环境
if [ ! -d "venv" ]; then
    echo "📦 创建虚拟环境..."
    python3 -m venv venv
fi

# 激活虚拟环境
echo "🔧 激活虚拟环境..."
source venv/bin/activate

# 检查并安装依赖
if [ ! -f "venv/.installed" ]; then
    echo "📥 安装依赖..."
    pip install --upgrade pip
    pip install -r requirements.txt
    touch venv/.installed
else
    echo "✅ 依赖已安装"
fi

# 检查环境变量文件
if [ ! -f ".env" ]; then
    echo "⚠️  警告: 找不到 .env 文件"
    echo "💡 提示: 请创建 apps/agent/.env 文件并配置环境变量"
    echo "💡 参考: apps/agent/.env.example (如果存在)"
fi

# 启动服务
echo "🌟 启动 FastAPI 服务..."
echo "📍 服务地址: http://localhost:8000"
echo "📚 API 文档: http://localhost:8000/docs"
echo ""
echo "按 Ctrl+C 停止服务"
echo ""

uvicorn main:app --reload --port 8000

