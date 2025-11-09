# SaleAgent 多智能体营销视频（Mono-repo）

前端 Next.js（Vercel）、后端 FastAPI + CrewAI（Railway），存储 Cloudflare R2，数据库 Supabase。

## 本地开发

### 前端（apps/web）
```bash
cd apps/web
pnpm i # 或 npm i / yarn
pnpm dev # http://localhost:3000
```

必须设置环境变量：
```
NEXT_PUBLIC_AGENT_URL=http://localhost:8000
NEXT_PUBLIC_SITE_URL=http://localhost:3000
# 若使用前端读取登录态（Supabase Auth）
NEXT_PUBLIC_SUPABASE_URL=https://xxxx.supabase.co
NEXT_PUBLIC_SUPABASE_ANON_KEY=eyJ...
```

### 后端（apps/agent）
```bash
cd apps/agent
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

环境变量（复制 .env.example 后填写）：
```
PROVIDER_IMAGE=qwen_runninghub   # qwen_runninghub | seedream | nanobanana
PROVIDER_VIDEO=pixverse          # pixverse | runninghub | sora2 | veo3.1 | hailuo

PIXVERSE_API_KEY=sk-...
RUNNINGHUB_API_KEY=...
RUNNINGHUB_WORKFLOW_ID=1985979937700159489
RUNNINGHUB_IMAGE_WORKFLOW_ID=

R2_ACCOUNT_ID=xxx
R2_ACCESS_KEY=xxx
R2_SECRET_KEY=xxx
R2_BUCKET=video

# Supabase（用于公开任务列表等）
SUPABASE_URL=https://xxxx.supabase.co
SUPABASE_ANON_KEY=eyJ...
CORS_ORIGIN=http://localhost:3000

# 可选：向量检索（用于相似模板推荐）- 使用 OpenRouter 统一管理不同模型服务商
EMBEDDING_API_BASE=https://openrouter.ai/api/v1
EMBEDDING_API_KEY=sk-or-v1-...  # OpenRouter API Key
EMBEDDING_MODEL=openai/text-embedding-3-small  # 或使用其他模型，如: nomic-ai/nomic-embed-text-v1.5
EMBEDDING_REFERER=https://saleagent.app  # OpenRouter 需要，用于标识应用来源
CF_WORKER_NOTIFY_URL=https://notify-worker.<your>.workers.dev
CF_NOTIFY_TOKEN=change-me
```

## 部署
- Vercel：选择 `apps/web`
- Railway：选择 `apps/agent`（Dockerfile 已配置）
- Cloudflare Worker（邮件通知）：
  1) 进入 `apps/notify-worker`，安装并发布：
     ```bash
     npm i -g wrangler
     cd apps/notify-worker
     wrangler publish
     ```
  2) 在 Cloudflare 域名配置 MailChannels 发信（SPF include 与 DKIM）。
  3) 记录 Worker URL，填到后端环境变量 `CF_WORKER_NOTIFY_URL`，并设置 `CF_NOTIFY_TOKEN`。

## 路由
- 前端：`/` 生成页、`/j/[slug]` 分享页、`/api/sitemap` 站点地图
- 后端：`POST /crewai-agent` SSE 事件流
  - `POST /jobs` 创建作业，返回 `run_id`/`share_slug`
  - `GET /jobs/{run_id}` 查询作业详情
  - `GET /share/{slug}` 分享页数据（SSR 使用）
  - `GET /public-jobs` 公开列表，支持 `page`/`limit`/`q`
  - `GET /recommend/{slug}` 相似模板推荐

## pgvector 与推荐（可选）
在 Supabase SQL 运行：
```sql
create extension if not exists vector;
create table if not exists prompts_library (
  id uuid primary key default gen_random_uuid(),
  category text,
  title text unique,
  prompt text,
  cover_url text,
  embedding vector(1536),
  created_at timestamptz default now()
);

-- 可选：创建 RPC，用于向量相似检索（若不创建，将自动回退为关键词检索）
-- 示例：
-- create or replace function match_prompts(query vector(1536), match_count int default 8)
-- returns table(id uuid, title text, category text, cover_url text, distance float)
-- language sql stable as $$
--   select p.id, p.title, p.category, p.cover_url, (p.embedding <-> query) as distance
--   from prompts_library p
--   where p.embedding is not null
--   order by p.embedding <-> query
--   limit match_count
-- $$;
```


