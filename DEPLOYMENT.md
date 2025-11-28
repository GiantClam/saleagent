# 部署指南

本项目采用 Monorepo 结构，前端部署在 Vercel，后端部署在 Railway。

## 项目结构

```
saleagent/
├── apps/
│   ├── web/          # Next.js 前端 → 部署到 Vercel
│   ├── agent/        # FastAPI 后端 → 部署到 Railway
│   └── notify-worker/ # Cloudflare Worker → 部署到 Cloudflare
└── README.md
```

---

## 1. Vercel 部署（前端）

### 1.1 准备工作

1. 在 [Vercel](https://vercel.com) 注册/登录账号
2. 连接你的 GitHub 仓库

### 1.2 部署步骤

1. **导入项目**
   - 在 Vercel Dashboard 点击 "Add New Project"
   - 选择你的 `saleagent` 仓库

2. **配置项目设置**
   - **Root Directory**: 设置为 `apps/web`
   - **Framework Preset**: 自动检测为 Next.js
   - **Build Command**: `npm run build` (或 `pnpm build` / `yarn build`)
   - **Output Directory**: `.next` (Next.js 默认)
   - **Install Command**: `npm install` (或 `pnpm install` / `yarn install`)
   
   > **注意**: 项目已包含 `apps/web/vercel.json` 配置文件，Vercel 会自动读取该配置。如果未自动检测到 Root Directory，请手动设置为 `apps/web`。

3. **环境变量配置**
   在 Vercel 项目设置中添加以下环境变量：
   ```
   NEXT_PUBLIC_AGENT_URL=https://your-app.railway.app
   NEXT_PUBLIC_SITE_URL=https://your-app.vercel.app
   NEXT_PUBLIC_SUPABASE_URL=https://xxxx.supabase.co
   NEXT_PUBLIC_SUPABASE_ANON_KEY=eyJ...
   ```

4. **部署**
   - 点击 "Deploy"
   - Vercel 会自动构建并部署

### 1.3 自动部署

- 推送到 `main` 分支会自动触发生产环境部署
- 其他分支会创建预览部署

### 1.4 自定义域名（可选）

在 Vercel 项目设置中配置自定义域名。

---

## 2. Railway 部署（后端）

### 2.1 准备工作

1. 在 [Railway](https://railway.app) 注册/登录账号
2. 连接你的 GitHub 仓库

### 2.2 部署步骤

1. **创建新项目**
   - 在 Railway Dashboard 点击 "New Project"
   - 选择 "Deploy from GitHub repo"
   - 选择你的 `saleagent` 仓库

2. **配置服务**
   - Railway 会自动检测到 `apps/agent/Dockerfile` 和 `apps/agent/railway.json`
   - 如果未自动检测，手动设置：
     - **Root Directory**: `apps/agent`
     - **Dockerfile Path**: `Dockerfile` (相对于 apps/agent)
   
   > **注意**: 项目已包含 `apps/agent/railway.json` 配置文件，Railway 会自动读取该配置以优化部署设置。

3. **环境变量配置**
   在 Railway 服务设置中添加以下环境变量：
   ```
   # 提供商配置
   PROVIDER_IMAGE=qwen_runninghub
   PROVIDER_VIDEO=pixverse
   
   # API Keys
   PIXVERSE_API_KEY=sk-...
   RUNNINGHUB_API_KEY=...
   RUNNINGHUB_WORKFLOW_ID=1985979937700159489
   RUNNINGHUB_IMAGE_WORKFLOW_ID=
   
   # Cloudflare R2
   R2_ACCOUNT_ID=xxx
   R2_ACCESS_KEY=xxx
   R2_SECRET_KEY=xxx
   R2_BUCKET=video
   
   # Supabase
   SUPABASE_URL=https://xxxx.supabase.co
   SUPABASE_ANON_KEY=eyJ...
   CORS_ORIGIN=https://your-app.vercel.app
   
   # 可选：向量检索（使用 OpenRouter 统一管理不同模型服务商）
   EMBEDDING_API_BASE=https://openrouter.ai/api/v1
   EMBEDDING_API_KEY=sk-or-v1-...  # OpenRouter API Key
   EMBEDDING_MODEL=openai/text-embedding-3-small  # 或使用其他模型，如: nomic-ai/nomic-embed-text-v1.5
   EMBEDDING_REFERER=https://your-app.vercel.app  # OpenRouter 需要，用于标识应用来源
   
   # Cloudflare Worker 通知
   CF_WORKER_NOTIFY_URL=https://notify-worker.xxx.workers.dev
   CF_NOTIFY_TOKEN=change-me
   ```

4. **端口配置**
   - Railway 会自动设置 `PORT` 环境变量
   - Dockerfile 中已配置监听 `0.0.0.0:8000`
   - Railway 会自动映射端口

5. **部署**
   - Railway 会自动检测 Dockerfile 并开始构建
   - 构建完成后会自动部署

### 2.3 获取部署 URL

- Railway 会自动生成一个公共 URL，格式：`https://your-app.railway.app`
- 将此 URL 配置到 Vercel 的 `NEXT_PUBLIC_AGENT_URL` 环境变量中

### 2.4 自定义域名（可选）

在 Railway 服务设置中配置自定义域名。

---

## 3. Cloudflare Worker 部署（通知服务）

### 3.1 部署步骤

1. **安装 Wrangler CLI**
   ```bash
   npm i -g wrangler
   ```

2. **登录 Cloudflare**
   ```bash
   wrangler login
   ```

3. **部署 Worker**
   ```bash
   cd apps/notify-worker
   wrangler publish
   ```

4. **配置 MailChannels**
   - 在 Cloudflare 域名设置中添加 MailChannels 的 SPF 记录
   - 配置 DKIM 密钥

5. **获取 Worker URL**
   - 部署后会显示 Worker URL，格式：`https://notify-worker.xxx.workers.dev`
   - 将此 URL 配置到 Railway 的 `CF_WORKER_NOTIFY_URL` 环境变量中

---

## 4. 部署检查清单

### Vercel（前端）
- [ ] 项目根目录设置为 `apps/web`
- [ ] `apps/web/vercel.json` 配置文件已存在（可选，但推荐）
- [ ] 环境变量已配置：
  - [ ] `NEXT_PUBLIC_AGENT_URL` (Railway 后端 URL)
  - [ ] `NEXT_PUBLIC_SITE_URL` (Vercel 前端 URL)
  - [ ] `NEXT_PUBLIC_SUPABASE_URL`
  - [ ] `NEXT_PUBLIC_SUPABASE_ANON_KEY`
- [ ] 构建成功
- [ ] 网站可访问

### Railway（后端）
- [ ] 根目录设置为 `apps/agent`
- [ ] `apps/agent/railway.json` 配置文件已存在（可选，但推荐）
- [ ] Dockerfile 已检测
- [ ] 环境变量已配置（见上方列表）
- [ ] `CORS_ORIGIN` 设置为 Vercel 前端 URL
- [ ] `GET /healthz` 探活返回 `{ ok: true }`
- [ ] 构建成功
- [ ] 服务运行正常
- [ ] 公共 URL 已获取并配置到前端

### Cloudflare Worker（可选）
- [ ] Worker 已部署
- [ ] MailChannels 已配置
- [ ] Worker URL 已配置到 Railway 环境变量

---

## 5. 常见问题

### 5.1 Vercel 构建失败

- 检查 Root Directory 是否正确设置为 `apps/web`
- 检查 Node.js 版本（建议 18+）
- 查看构建日志中的具体错误

### 5.2 Railway 部署失败

- 检查 Dockerfile 路径是否正确
- 检查环境变量是否完整
- 查看 Railway 日志中的错误信息
- 确认 `requirements.txt` 中的依赖是否正确

### 5.3 CORS 错误

- 确保 Railway 的 `CORS_ORIGIN` 环境变量设置为 Vercel 的前端 URL
- 检查前端 `NEXT_PUBLIC_AGENT_URL` 是否正确

### 5.4 环境变量未生效

- Vercel: 修改环境变量后需要重新部署
- Railway: 修改环境变量后会自动重启服务

---

## 6. 持续集成/持续部署 (CI/CD)

### Vercel
- 自动部署：推送到 `main` 分支自动触发生产部署
- 预览部署：其他分支自动创建预览环境

### Railway
- 自动部署：推送到连接的 GitHub 分支自动触发部署
- 可在 Railway 设置中配置自动部署的分支

---

## 7. 监控和日志

### Vercel
- 在 Vercel Dashboard 查看部署日志和构建日志
- 查看函数日志和边缘函数日志

### Railway
- 在 Railway Dashboard 查看服务日志
- 实时查看应用输出和错误日志

---

## 8. 回滚

### Vercel
- 在部署历史中选择之前的部署版本进行回滚

### Railway
- 在部署历史中选择之前的版本进行回滚
- 或通过 Git 回退到之前的提交

---

## 9. 成本优化建议

1. **Vercel**
   - Hobby 计划适合小型项目
   - 注意函数执行时间和调用次数限制

2. **Railway**
   - 使用 $5/月的 Starter 计划
   - 注意资源使用量（CPU、内存、带宽）
   - 可以设置自动休眠以节省成本

3. **Cloudflare Worker**
   - 免费计划包含 100,000 次请求/天
   - 超出后按使用量计费

---

## 10. 安全建议

1. **环境变量**
- 不要在代码中硬编码敏感信息
- 使用平台的环境变量管理功能
- 定期轮换 API 密钥
 - 本仓库未硬编码密钥，敏感项仅存在于 `.env.example` 作为占位（如 `OPENROUTER_API_KEY`、`SUPABASE_SERVICE_ROLE_KEY`、`PIXVERSE_API_KEY`、`RUNNINGHUB_API_KEY`、`R2_SECRET_KEY`）。
 - 前端仅使用 `NEXT_PUBLIC_*` 变量暴露非敏感值，服务端密钥仅在后端配置。

2. **CORS**
- 限制 `CORS_ORIGIN` 为具体的域名，避免使用 `*`

3. **API 密钥**
   - 使用强密码和随机生成的 token
   - 定期检查 API 密钥的使用情况

---

## 11. 更新部署

### 更新前端
```bash
git add .
git commit -m "Update frontend"
git push origin main
# Vercel 会自动部署
```

### 更新后端
```bash
git add .
git commit -m "Update backend"
git push origin main
# Railway 会自动部署
```

---

## 12. 本地测试部署配置

在部署前，建议在本地测试：

```bash
# 测试前端构建
cd apps/web
npm run build
npm start

# 测试后端构建
cd apps/agent
docker build -t saleagent-agent .
docker run -p 8000:8000 --env-file .env saleagent-agent
```

---

## 联系和支持

如有部署问题，请检查：
1. 各平台的官方文档
2. 项目的 GitHub Issues
3. 构建和运行日志

