## 总览
- 后端使用 FastAPI，已提供完整 REST/SSE 接口，并通过 `dotenv` 在进程启动前加载环境变量（apps/agent/main.py:13–16）。
- CORS 已内置，生产需将 `CORS_ORIGIN` 配置为前端域（apps/agent/main.py:71–78）。
- Railway 采用 `apps/agent/Dockerfile` 构建部署（apps/agent/railway.json:1–11），当前监听 `8000` 端口（apps/agent/Dockerfile:9–10）。
- 前端 Next.js 使用 `NEXT_PUBLIC_AGENT_URL` 访问后端（apps/web/.env.example:4；apps/web/app/workflow/WorkflowContent.tsx:20,54,81,104,131 等）。

## 需要的代码/配置调整
1. 端口兼容 Railway（建议）
- 目的：保证在 Docker 与非 Docker（Nixpacks）两种模式下都能正确监听 Railway 提供的 `PORT`。
- 方案：将 Docker 启动命令改为 shell 形式，优先读取 `PORT`，默认 `8000`。
- 变更：apps/agent/Dockerfile:10 → `CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --timeout-keep-alive 180"]`

2. 健康检查端点（建议）
- 目的：利于 Railway/监控探活与前端排查。
- 方案：在 FastAPI 增加 `/healthz` 返回 `{ok:true}`。
- 位置：apps/agent/main.py（与现有路由同级，示例参考 apps/agent/main.py 的其他 `@app.get`）。

3. CORS 生产化设置（必须）
- 将 `CORS_ORIGIN` 设置为前端实际域名，避免通配 `*`（apps/agent/main.py:71–78）。

4. 前端对接配置（必须）
- 在前端平台（Vercel 或 Railway）设置：
  - `NEXT_PUBLIC_AGENT_URL=https://{railway-app}.railway.app`（apps/web/.env.example:4）。
  - `NEXT_PUBLIC_SITE_URL=https://{frontend-domain}`（apps/web/.env.example:9）。
  - `NEXT_PUBLIC_SUPABASE_URL`、`NEXT_PUBLIC_SUPABASE_ANON_KEY`（apps/web/.env.example:15–16）。

## Railway 部署步骤（后端）
1. 在 Railway 创建项目并关联仓库；Root 指向 `apps/agent`。
2. 检测 `railway.json` 与 `Dockerfile`，使用 Docker 构建（apps/agent/railway.json:3–6；apps/agent/Dockerfile:1–10）。
3. 在 Railway 服务的环境变量中配置：
- 提供商与 API Key：`PROVIDER_IMAGE`、`PROVIDER_VIDEO`、`PIXVERSE_API_KEY`、`RUNNINGHUB_API_KEY`、`RUNNINGHUB_WORKFLOW_ID` 等（apps/agent/.env.example:4–17）。
- R2 存储：`R2_ACCOUNT_ID`、`R2_ACCESS_KEY`、`R2_SECRET_KEY`、`R2_BUCKET`、`R2_PUBLIC_BASE`（apps/agent/.env.example:21–26）。
- Supabase：`SUPABASE_URL`、`SUPABASE_SERVICE_ROLE_KEY` 或 `SUPABASE_SERVICE_KEY`（apps/agent/main.py:106–115；apps/agent/.env.example:30–33）。
- OpenRouter：`OPENROUTER_API_BASE`、`OPENROUTER_API_KEY`、`EMBEDDING_MODEL`、`EMBEDDING_REFERER`（apps/agent/main.py:115–133；apps/agent/.env.example:37–43）。
- CORS：`CORS_ORIGIN=https://{frontend-domain}`（apps/agent/.env.example:47）。
- Cloudflare Worker（可选）：`CF_WORKER_NOTIFY_URL`、`CF_NOTIFY_TOKEN`（apps/agent/.env.example:52–54）。

## 前端接口可用性（确认清单）
- 主要接口：
  - 规划：`POST /workflow/plan`（apps/agent/main.py:278–314）。
  - 关键帧：`POST /workflow/keyframes`（apps/agent/main.py:325–333）。
  - 确认并持久化：`POST /workflow/confirm`（apps/agent/main.py:343–367）。
  - 生成片段（SSE）：`POST /workflow/run-clips`（apps/agent/main.py:386–459）。
  - 拼接：`POST /workflow/stitch`（apps/agent/main.py:477–505）。
  - Crew 工作流：`POST /workflow/crew-run`、`GET /workflow/crew-status/{run_id}`（apps/agent/main.py:516–693, 696–735）。
  - 资源查询：`GET /public-jobs`、`GET /my-jobs`、`GET /jobs/{run_id}`、`GET /share/{slug}`（apps/agent/main.py:1276–1413, 1381–1386, 1408–1413）。
- 前端已对接上述路径并处理 SSE（apps/web/app/workflow/WorkflowContent.tsx:123–189, 145–153）。

## 敏感信息检查与安全建议
- 结果：仓库未发现硬编码密钥，敏感项通过环境变量读取（如 `OPENROUTER_API_KEY`、`SUPABASE_SERVICE_ROLE_KEY`、`PIXVERSE_API_KEY`、`RUNNINGHUB_API_KEY`），仅在 `.env.example` 中占位（apps/agent/.env.example:10–54）。
- `.gitignore` 已忽略 `.env*`，降低泄漏风险（.gitignore:79–86, 116–121）。
- 后端仅在服务端使用服务密钥；前端将 Supabase 仅以 `NEXT_PUBLIC_*` 公开（apps/web/.env.example:15–16）。
- 建议：
  - 将 `CORS_ORIGIN` 限制为生产域；避免 `*`。
  - 在平台侧设置与轮换 API Key，避免写入代码库。
  - 检查日志输出，不记录密钥值（apps/agent/main.py:80–105 配置为 stdout + 旋转文件，未打印敏感变量）。

## 上线验证
- 后端：
  - 打开 `https://{railway-app}.railway.app/docs` 确认接口文档加载正常（FastAPI 自动文档）。
  - 调用 `GET /public-jobs` 与 `POST /workflow/plan` 验证基础读写与外部依赖（OpenRouter）。
- 前端：
  - 配置 `NEXT_PUBLIC_AGENT_URL` 指向 Railway URL；刷新页面，检查工作流各阶段是否正常。
  - 若跨域报错，确认 Railway 的 `CORS_ORIGIN` 是否为前端实际域。

## 备用与排错
- 若 Railway 未使用 Docker 而是 Nixpacks：端口必须读取 `PORT` 环境，否则会 502；本计划包含 Dockerfile 兼容修改以消除该风险。
- 外部依赖（Supabase、R2、OpenRouter、Pixverse/RunningHub）未配置时，部分接口会返回错误或降级；参考错误信息与 `.env.example` 完成配置。