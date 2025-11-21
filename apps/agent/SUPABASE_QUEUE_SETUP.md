# Supabase 任务队列设置指南

## 概述

Supabase 任务队列是一个基于数据库的持久化任务队列系统，用于管理视频生成任务。它提供了以下优势：

1. **持久化**：任务不会因为服务重启而丢失
2. **多实例支持**：可以在多个 Railway 实例间共享队列
3. **可靠性**：数据库事务保证数据一致性
4. **免费版通常足够**：Supabase 免费版提供 500MB 数据库存储

## 成本分析

- **Supabase 免费版**：500MB 数据库存储，通常足够小到中等规模使用
- **如果已有 Supabase 账号**：无需额外付费
- **如果任务量很大**：可能需要升级到付费计划

## 设置步骤

### 1. 创建数据库表

在 Supabase SQL Editor 中执行以下 SQL：

```sql
-- 创建 video_tasks 表
CREATE TABLE IF NOT EXISTS video_tasks (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id TEXT NOT NULL,
  clip_idx INTEGER NOT NULL,
  prompt TEXT NOT NULL,
  ref_img TEXT,
  duration INTEGER DEFAULT 10,
  status TEXT DEFAULT 'pending',  -- pending, processing, submitted, succeeded, failed
  provider_task_id TEXT,  -- RunningHub task_id
  video_url TEXT,
  error TEXT,
  retry_count INTEGER DEFAULT 0,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 创建索引
CREATE INDEX IF NOT EXISTS idx_video_tasks_run_id ON video_tasks(run_id);
CREATE INDEX IF NOT EXISTS idx_video_tasks_status ON video_tasks(status);
CREATE INDEX IF NOT EXISTS idx_video_tasks_created_at ON video_tasks(created_at);
CREATE INDEX IF NOT EXISTS idx_video_tasks_provider_task_id ON video_tasks(provider_task_id);

-- 添加约束
ALTER TABLE video_tasks DROP CONSTRAINT IF EXISTS check_status;
ALTER TABLE video_tasks ADD CONSTRAINT check_status 
  CHECK (status IN ('pending', 'processing', 'submitted', 'succeeded', 'failed'));

-- 自动更新 updated_at 的触发器
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

DROP TRIGGER IF EXISTS update_video_tasks_updated_at ON video_tasks;
CREATE TRIGGER update_video_tasks_updated_at
    BEFORE UPDATE ON video_tasks
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();
```

或者直接执行 `apps/agent/supabase_queue_schema.sql` 文件。

### 2. 配置环境变量

在 Railway 或本地 `.env` 文件中添加：

```bash
# Supabase 配置（必需）
SUPABASE_URL=https://xxxxx.supabase.co
SUPABASE_SERVICE_ROLE_KEY=eyJ...  # 使用 Service Role Key，不要使用 Anon Key
```

**重要**：必须使用 `SUPABASE_SERVICE_ROLE_KEY`（服务密钥），而不是 `SUPABASE_ANON_KEY`（匿名密钥），因为后端需要插入和更新数据。

### 3. 验证配置

启动服务后，检查日志中是否有以下信息：

```
[SupabaseVideoTaskQueue] Table 'video_tasks' exists
[SupabaseVideoTaskQueue] Worker loop started
```

如果看到警告信息，说明表不存在或配置有误。

## 工作原理

### 任务提交流程

1. **生成视频片段工具** (`generate_video_clip_tool`) 被调用
2. 如果 Supabase 队列可用，任务被添加到 `video_tasks` 表，状态为 `pending`
3. 后台 worker 循环（每 10 秒）检查 `pending` 任务
4. Worker 调用视频生成 provider 提交任务到 RunningHub
5. 任务状态更新为 `submitted`，并记录 `provider_task_id`
6. Worker 继续轮询 RunningHub 状态，直到任务完成
7. 任务完成后，视频 URL 上传到 R2，状态更新为 `succeeded`

### 任务轮询流程

1. **拼接视频工具** (`stitch_video_tool`) 被调用
2. 如果发现 `pending` 任务，根据 `queue_type` 选择轮询方式：
   - `supabase`：使用队列的 `poll_task_status` 方法
   - `direct`：直接轮询 RunningHub API
3. 轮询最多 60 次（5 分钟），每次间隔 5 秒
4. 任务完成后，返回视频 URL 进行拼接

### 自动重试机制

- 如果 RunningHub 返回 `TASK_QUEUE_MAXED` 错误，任务状态会重置为 `pending`
- Worker 会在下次循环时重新尝试提交
- 最多重试 10 次，超过后标记为 `failed`

## 降级机制

如果 Supabase 队列不可用（未配置或初始化失败），系统会自动降级到直接提交模式：

- 任务直接提交到 RunningHub，不经过数据库队列
- 轮询时直接调用 RunningHub API
- 功能不受影响，但失去了持久化和多实例支持

## 监控和调试

### 查看队列状态

在 Supabase Dashboard 中查询：

```sql
-- 查看所有待处理任务
SELECT * FROM video_tasks WHERE status = 'pending' ORDER BY created_at;

-- 查看失败的任务
SELECT * FROM video_tasks WHERE status = 'failed' ORDER BY created_at DESC LIMIT 10;

-- 查看指定 run_id 的所有任务
SELECT * FROM video_tasks WHERE run_id = 'your-run-id' ORDER BY clip_idx;
```

### 日志关键字

- `[SupabaseVideoTaskQueue]`：队列相关日志
- `[generate_video_clip_tool]`：任务提交日志
- `[stitch_video_tool]`：任务轮询日志

## 故障排除

### 问题 1：表不存在

**症状**：日志中出现 `Table 'video_tasks' may not exist`

**解决**：执行步骤 1 中的 SQL 创建表

### 问题 2：权限错误

**症状**：日志中出现 `Failed to add task` 或 `permission denied`

**解决**：
1. 确保使用 `SUPABASE_SERVICE_ROLE_KEY` 而不是 `SUPABASE_ANON_KEY`
2. 检查 Supabase 项目设置中的 RLS（Row Level Security）策略

### 问题 3：任务一直处于 pending 状态

**可能原因**：
1. Worker 循环未启动（检查日志）
2. 视频生成 provider 配置错误
3. RunningHub API 调用失败

**解决**：
1. 检查日志中的错误信息
2. 手动查询数据库查看任务详情
3. 检查环境变量配置

## 性能优化

### 调整 Worker 参数

在 `video_task_queue_supabase.py` 中修改：

```python
queue = SupabaseVideoTaskQueue(
    retry_interval=10.0,  # 重试间隔（秒）
    max_concurrent=2      # 最大并发数
)
```

### 数据库索引

已创建的索引：
- `idx_video_tasks_run_id`：按 run_id 查询
- `idx_video_tasks_status`：按状态查询
- `idx_video_tasks_created_at`：按创建时间排序
- `idx_video_tasks_provider_task_id`：按 provider_task_id 查询

## 注意事项

1. **Service Role Key 安全**：Service Role Key 具有完全访问权限，不要在前端代码中使用
2. **数据库存储**：定期清理旧任务，避免数据库过大
3. **并发控制**：`max_concurrent` 不要设置过大，避免 RunningHub 队列溢出
4. **重试间隔**：`retry_interval` 不要设置过小，避免频繁查询数据库

## 清理旧任务（可选）

定期清理已完成或失败的任务：

```sql
-- 删除 7 天前的已完成任务
DELETE FROM video_tasks 
WHERE status IN ('succeeded', 'failed') 
AND created_at < NOW() - INTERVAL '7 days';

-- 删除 30 天前的所有任务
DELETE FROM video_tasks 
WHERE created_at < NOW() - INTERVAL '30 days';
```

