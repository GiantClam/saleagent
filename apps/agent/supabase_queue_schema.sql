-- Supabase 视频任务队列表结构
-- 在 Supabase SQL Editor 中执行此 SQL 创建表

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

-- 可选：自动更新 updated_at 的触发器
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

