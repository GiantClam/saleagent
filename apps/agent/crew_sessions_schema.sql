-- CrewAI 会话管理表结构
-- 在 Supabase SQL Editor 中执行此 SQL 创建表

CREATE TABLE IF NOT EXISTS crew_sessions (
  run_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  expected_clips INTEGER NOT NULL,
  status TEXT DEFAULT 'waiting_videos',  -- running, waiting_videos, ready_to_stitch, stitching, completed, failed
  context JSONB,  -- 存储上下文信息（goal, styles, total_duration 等）
  result TEXT,  -- 最终视频 URL
  error TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 创建索引
CREATE INDEX IF NOT EXISTS idx_crew_sessions_session_id ON crew_sessions(session_id);
CREATE INDEX IF NOT EXISTS idx_crew_sessions_status ON crew_sessions(status);
CREATE INDEX IF NOT EXISTS idx_crew_sessions_created_at ON crew_sessions(created_at);

-- 自动更新 updated_at 的触发器
CREATE OR REPLACE FUNCTION update_crew_sessions_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

DROP TRIGGER IF EXISTS update_crew_sessions_updated_at ON crew_sessions;
CREATE TRIGGER update_crew_sessions_updated_at
    BEFORE UPDATE ON crew_sessions
    FOR EACH ROW
    EXECUTE FUNCTION update_crew_sessions_updated_at();

