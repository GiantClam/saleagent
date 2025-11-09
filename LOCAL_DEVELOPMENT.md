# 本地开发运行指南

本项目可以在本地运行。本文档提供详细的本地运行步骤和配置说明。

## 📋 前置要求

### 必需
- **Node.js** 18+ 和 npm/pnpm/yarn
- **Python** 3.11+
- **Git**

### 可选（用于完整功能）
- **Supabase** 账号（用于数据库和认证）
- **Cloudflare R2** 账号（用于文件存储）
- **API Keys**（用于图片/视频生成服务）

---

## 🚀 快速开始

### 方法一：使用启动脚本（推荐）

项目提供了便捷的启动脚本，可以自动处理环境设置：

```bash
# 克隆项目
git clone <your-repo-url>
cd saleagent

# 启动后端（在第一个终端）
./scripts/start-backend.sh

# 启动前端（在第二个终端）
./scripts/start-frontend.sh
```

启动脚本会自动：
- 检查并创建虚拟环境（后端）
- 安装依赖
- 检查环境变量文件
- 启动服务

### 方法二：手动启动

### 1. 克隆项目

```bash
git clone <your-repo-url>
cd saleagent
```

### 2. 启动后端服务

```bash
# 进入后端目录
cd apps/agent

# 创建虚拟环境（推荐）
python -m venv venv

# 激活虚拟环境
# macOS/Linux:
source venv/bin/activate
# Windows:
# venv\Scripts\activate

# 安装依赖
pip install -r requirements.txt

# 创建环境变量文件
cp .env.example .env  # 如果存在
# 或手动创建 .env 文件

# 启动服务（开发模式，支持热重载）
uvicorn main:app --reload --port 8000
```

后端服务将在 `http://localhost:8000` 启动。

**重要提示**: 
- 必须在 `apps/agent` 目录下运行 `uvicorn` 命令
- 如果遇到相对导入错误（`ModuleNotFoundError: No module named 'apps.agent.providers'`），请确保：
  1. 当前工作目录是 `apps/agent`
  2. 或者设置 `PYTHONPATH`:
     ```bash
     export PYTHONPATH=$PWD:$PYTHONPATH  # macOS/Linux
     set PYTHONPATH=%CD%;%PYTHONPATH%    # Windows
     ```
  3. 或者使用 `python -m uvicorn main:app` 代替 `uvicorn main:app`

### 3. 启动前端服务

打开新的终端窗口：

```bash
# 进入前端目录
cd apps/web

# 安装依赖
npm install
# 或使用 pnpm:
# pnpm install
# 或使用 yarn:
# yarn install

# 创建环境变量文件
# 在 apps/web 目录下创建 .env.local 文件

# 启动开发服务器
npm run dev
# 或使用 pnpm:
# pnpm dev
# 或使用 yarn:
# yarn dev
```

前端服务将在 `http://localhost:3000` 启动。

---

## ⚙️ 环境变量配置

### 后端环境变量 (`apps/agent/.env`)

创建 `apps/agent/.env` 文件：

```bash
# ============================================
# 提供商配置（必需至少配置一个）
# ============================================
PROVIDER_IMAGE=qwen_runninghub   # 选项: qwen_runninghub | seedream | nanobanana
PROVIDER_VIDEO=pixverse          # 选项: pixverse | runninghub | sora2 | veo3.1 | hailuo

# ============================================
# API Keys（根据选择的提供商配置）
# ============================================
# Pixverse API Key
PIXVERSE_API_KEY=sk-...

# RunningHub API Key
RUNNINGHUB_API_KEY=...
RUNNINGHUB_WORKFLOW_ID=1985979937700159489
RUNNINGHUB_IMAGE_WORKFLOW_ID=

# ============================================
# Cloudflare R2 存储配置
# ============================================
R2_ACCOUNT_ID=xxx
R2_ACCESS_KEY=xxx
R2_SECRET_KEY=xxx
R2_BUCKET=video

# ============================================
# Supabase 配置（用于数据库和认证）
# ============================================
SUPABASE_URL=https://xxxx.supabase.co
SUPABASE_ANON_KEY=eyJ...

# ============================================
# CORS 配置（必需）
# ============================================
CORS_ORIGIN=http://localhost:3000

# ============================================
# 可选：向量检索（用于相似模板推荐）- 使用 OpenRouter 统一管理不同模型服务商
# ============================================
EMBEDDING_API_BASE=https://openrouter.ai/api/v1
EMBEDDING_API_KEY=sk-or-v1-...  # OpenRouter API Key
EMBEDDING_MODEL=openai/text-embedding-3-small  # 或使用其他模型，如: nomic-ai/nomic-embed-text-v1.5
EMBEDDING_REFERER=http://localhost:3000  # OpenRouter 需要，用于标识应用来源

# ============================================
# 可选：Cloudflare Worker 通知
# ============================================
CF_WORKER_NOTIFY_URL=https://notify-worker.xxx.workers.dev
CF_NOTIFY_TOKEN=change-me
```

### 前端环境变量 (`apps/web/.env.local`)

创建 `apps/web/.env.local` 文件：

```bash
# ============================================
# 后端 API 地址（必需）
# ============================================
NEXT_PUBLIC_AGENT_URL=http://localhost:8000

# ============================================
# 前端地址（必需）
# ============================================
NEXT_PUBLIC_SITE_URL=http://localhost:3000

# ============================================
# Supabase 配置（用于前端认证）
# ============================================
NEXT_PUBLIC_SUPABASE_URL=https://xxxx.supabase.co
NEXT_PUBLIC_SUPABASE_ANON_KEY=eyJ...
```

---

## 🔧 最小化配置（仅测试运行）

如果只是想测试项目能否运行，可以使用最小配置：

### 后端最小配置 (`apps/agent/.env`)

```bash
# 最小配置 - 使用 Mock 提供商
PROVIDER_IMAGE=  # 留空会使用 MockImageProvider
PROVIDER_VIDEO=  # 留空会使用 MockVideoProvider
CORS_ORIGIN=http://localhost:3000
```

Mock 提供商会返回示例图片和视频，不会调用真实的 API。

### 前端最小配置 (`apps/web/.env.local`)

```bash
NEXT_PUBLIC_AGENT_URL=http://localhost:8000
NEXT_PUBLIC_SITE_URL=http://localhost:3000
```

**注意**: 某些功能（如用户认证、数据持久化）需要 Supabase 配置。

---

## 📝 详细步骤

### 步骤 1: 准备 Python 环境

```bash
cd apps/agent

# 创建虚拟环境
python3 -m venv venv

# 激活虚拟环境
source venv/bin/activate  # macOS/Linux
# 或
venv\Scripts\activate     # Windows

# 升级 pip
pip install --upgrade pip

# 安装依赖
pip install -r requirements.txt
```

### 步骤 2: 配置后端环境变量

```bash
# 在 apps/agent 目录下创建 .env 文件
touch .env  # macOS/Linux
# 或
type nul > .env  # Windows

# 编辑 .env 文件，填入必要的配置
# 参考上面的环境变量配置示例
```

### 步骤 3: 启动后端

```bash
# 确保在 apps/agent 目录下
cd apps/agent

# 确保虚拟环境已激活
source venv/bin/activate  # macOS/Linux

# 启动服务
uvicorn main:app --reload --port 8000
```

**验证后端是否运行**:
- 打开浏览器访问 `http://localhost:8000/docs` 查看 API 文档
- 或访问 `http://localhost:8000` 查看根路径响应

### 步骤 4: 准备 Node.js 环境

```bash
# 进入前端目录
cd apps/web

# 安装依赖
npm install
```

### 步骤 5: 配置前端环境变量

```bash
# 在 apps/web 目录下创建 .env.local 文件
touch .env.local  # macOS/Linux
# 或
type nul > .env.local  # Windows

# 编辑 .env.local 文件，填入必要的配置
# 参考上面的环境变量配置示例
```

### 步骤 6: 启动前端

```bash
# 确保在 apps/web 目录下
cd apps/web

# 启动开发服务器
npm run dev
```

**验证前端是否运行**:
- 打开浏览器访问 `http://localhost:3000`
- 应该能看到应用界面

---

## 🐛 常见问题排查

### 问题 1: 后端导入错误

**错误信息**:
```
ModuleNotFoundError: No module named 'apps.agent.providers'
```

**解决方案**:
1. 确保在 `apps/agent` 目录下运行 `uvicorn` 命令
2. 或者设置 `PYTHONPATH`:
   ```bash
   export PYTHONPATH=$PWD:$PYTHONPATH  # macOS/Linux
   set PYTHONPATH=%CD%;%PYTHONPATH%    # Windows
   ```

### 问题 2: 前端无法连接到后端

**错误信息**:
```
Failed to fetch
Network error
```

**解决方案**:
1. 确认后端服务正在运行（访问 `http://localhost:8000/docs`）
2. 检查 `NEXT_PUBLIC_AGENT_URL` 是否正确设置为 `http://localhost:8000`
3. 检查后端的 `CORS_ORIGIN` 是否包含 `http://localhost:3000`
4. 检查防火墙设置

### 问题 3: 端口已被占用

**错误信息**:
```
Address already in use
Port 8000 is already in use
```

**解决方案**:
1. 查找占用端口的进程:
   ```bash
   lsof -i :8000  # macOS/Linux
   netstat -ano | findstr :8000  # Windows
   ```
2. 终止进程或使用其他端口:
   ```bash
   uvicorn main:app --reload --port 8001  # 后端
   npm run dev -- -p 3001  # 前端
   ```
3. 记得更新环境变量中的端口号

### 问题 4: Python 依赖安装失败

**错误信息**:
```
ERROR: Could not find a version that satisfies the requirement
```

**解决方案**:
1. 升级 pip: `pip install --upgrade pip`
2. 使用国内镜像源:
   ```bash
   pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
   ```
3. 检查 Python 版本是否为 3.11+

### 问题 5: Node.js 依赖安装失败

**错误信息**:
```
npm ERR! code ELIFECYCLE
```

**解决方案**:
1. 清除缓存: `npm cache clean --force`
2. 删除 `node_modules` 和 `package-lock.json`，重新安装
3. 尝试使用 `pnpm` 或 `yarn`

### 问题 6: 环境变量未生效

**解决方案**:
1. **后端**: 确保 `.env` 文件在 `apps/agent` 目录下
2. **前端**: 确保 `.env.local` 文件在 `apps/web` 目录下
3. **前端**: Next.js 的环境变量必须以 `NEXT_PUBLIC_` 开头才能在浏览器中使用
4. 重启开发服务器

---

## 🔍 验证运行状态

### 检查后端

1. **API 文档**: 访问 `http://localhost:8000/docs`
2. **健康检查**: 访问 `http://localhost:8000` 或 `http://localhost:8000/health`（如果有）
3. **测试端点**: 
   ```bash
   curl http://localhost:8000/public-jobs
   ```

### 检查前端

1. **访问首页**: `http://localhost:3000`
2. **检查控制台**: 打开浏览器开发者工具，查看是否有错误
3. **检查网络请求**: 在 Network 标签页查看 API 请求是否成功

---

## 🛠️ 开发工具推荐

### VS Code 扩展
- Python
- ESLint
- Prettier
- REST Client（用于测试 API）

### 有用的命令

```bash
# 后端
cd apps/agent
uvicorn main:app --reload --port 8000  # 开发模式
uvicorn main:app --host 0.0.0.0 --port 8000  # 生产模式

# 前端
cd apps/web
npm run dev        # 开发模式
npm run build      # 构建生产版本
npm run start      # 运行生产版本
npm run lint       # 代码检查
```

---

## 📚 项目结构说明

```
saleagent/
├── apps/
│   ├── web/              # Next.js 前端应用
│   │   ├── app/          # Next.js App Router
│   │   ├── .env.local    # 前端环境变量（需创建）
│   │   └── package.json
│   │
│   ├── agent/            # FastAPI 后端应用
│   │   ├── main.py       # FastAPI 应用入口
│   │   ├── providers.py  # 提供商抽象
│   │   ├── .env          # 后端环境变量（需创建）
│   │   └── requirements.txt
│   │
│   └── notify-worker/    # Cloudflare Worker（可选）
│
├── README.md
├── DEPLOYMENT.md
└── LOCAL_DEVELOPMENT.md  # 本文档
```

---

## 🎯 下一步

1. **配置真实 API**: 替换 Mock 提供商为真实的图片/视频生成服务
2. **配置 Supabase**: 设置数据库和用户认证
3. **配置 R2**: 设置文件存储
4. **阅读代码**: 了解项目架构和功能
5. **开始开发**: 根据需求修改和扩展功能

---

## 💡 提示

- 使用虚拟环境可以避免 Python 包冲突
- 使用 `.env.local` 文件（前端）和 `.env` 文件（后端）管理环境变量，不要提交到 Git
- 开发时使用 `--reload` 参数可以自动重载代码更改
- 查看 `README.md` 了解更多项目信息
- 查看 `DEPLOYMENT.md` 了解如何部署到生产环境

---

## 🆘 获取帮助

如果遇到问题：
1. 检查本文档的"常见问题排查"部分
2. 查看项目的 GitHub Issues
3. 检查各服务的官方文档
4. 查看代码注释和 README

