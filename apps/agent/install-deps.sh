#!/bin/bash
# 分步安装依赖，解决 resolution-too-deep 问题

set -e

echo "📦 分步安装依赖（使用官方 PyPI 源）..."

# 使用官方 PyPI 源，避免镜像源同步问题
PIP_INDEX="--index-url https://pypi.org/simple/"

# 先安装基础依赖
echo "1️⃣ 安装基础依赖..."
pip install $PIP_INDEX fastapi==0.115.0
pip install $PIP_INDEX uvicorn==0.30.3
pip install $PIP_INDEX httpx==0.27.0
pip install $PIP_INDEX "python-dotenv>=1.1.1,<2.0.0"
pip install $PIP_INDEX orjson==3.10.7
pip install $PIP_INDEX sse-starlette==1.8.2

# 安装 AWS 相关
echo "2️⃣ 安装 AWS 相关依赖..."
pip install $PIP_INDEX boto3==1.34.153

# 安装 Supabase
echo "3️⃣ 安装 Supabase..."
pip install $PIP_INDEX supabase==2.5.1

# 最后安装 CrewAI（使用 legacy resolver 避免依赖解析问题）
echo "4️⃣ 安装 CrewAI（这可能需要一些时间）..."
pip install $PIP_INDEX --use-deprecated=legacy-resolver crewai==1.4.0

echo "✅ 所有依赖安装完成！"

