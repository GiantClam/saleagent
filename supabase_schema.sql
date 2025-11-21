-- ============================================
-- SaleAgent Supabase 数据库表结构
-- ============================================
-- 使用方法：
-- 1. 登录 Supabase Dashboard
-- 2. 进入你的项目
-- 3. 点击左侧菜单 "SQL Editor"
-- 4. 点击 "New query"
-- 5. 复制粘贴以下 SQL 并执行
-- ============================================

-- ============================================
-- 1. jobs 表（必需）- 存储视频生成任务
-- ============================================
CREATE TABLE IF NOT EXISTS jobs (
  run_id TEXT PRIMARY KEY,
  user_id UUID,
  slogan TEXT,
  cover_url TEXT,
  video_url TEXT,
  share_slug TEXT UNIQUE,
  status TEXT DEFAULT 'running',
  provider_task_id TEXT,
  storyboards JSONB,
  total_duration REAL,
  styles TEXT[],
  image_control BOOLEAN DEFAULT FALSE,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 创建索引以提升查询性能
CREATE INDEX IF NOT EXISTS idx_jobs_user_id ON jobs(user_id);
CREATE INDEX IF NOT EXISTS idx_jobs_share_slug ON jobs(share_slug);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_provider_task_id ON jobs(provider_task_id);
CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at DESC);

-- ============================================
-- 2. prompts_library 表（可选）- 营销模板库
-- ============================================
-- 注意：此表需要 pgvector 扩展，用于向量相似度搜索
-- 如果不需要模板推荐功能，可以跳过此表

-- 首先启用 pgvector 扩展
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS prompts_library (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  category TEXT,
  title TEXT UNIQUE,
  prompt TEXT,
  cover_url TEXT,
  embedding vector(1536),  -- 向量维度：openai/text-embedding-3-small 是 1536 维
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 创建向量索引（可选，提升向量检索性能）
-- 注意：只有当表中有数据时才能创建索引
CREATE INDEX IF NOT EXISTS idx_prompts_embedding ON prompts_library 
USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- 创建其他索引
CREATE INDEX IF NOT EXISTS idx_prompts_title ON prompts_library(title);
CREATE INDEX IF NOT EXISTS idx_prompts_category ON prompts_library(category);

-- ============================================
-- 3. profiles 表（可选）- 用户扩展信息
-- ============================================
-- 注意：Supabase Auth 会自动创建 auth.users 表
-- 此表用于存储额外的用户信息

CREATE TABLE IF NOT EXISTS profiles (
  id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  email TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 创建索引
CREATE INDEX IF NOT EXISTS idx_profiles_email ON profiles(email);

-- ============================================
-- 4. Row Level Security (RLS) 策略（可选但推荐）
-- ============================================
-- 启用 RLS 以保护数据安全
-- 
-- 注意：
-- - 如果后端使用 SERVICE_KEY，可以绕过 RLS（推荐用于生产环境）
-- - 如果后端使用 ANON_KEY，需要以下策略允许后端操作
-- - 建议后端使用 SERVICE_KEY，前端使用 ANON_KEY

-- jobs 表 RLS
ALTER TABLE jobs ENABLE ROW LEVEL SECURITY;

-- 策略：用户只能查看自己的任务
CREATE POLICY "Users can view their own jobs"
  ON jobs FOR SELECT
  USING (auth.uid() = user_id);

-- 策略：允许后端服务插入任务（使用 SERVICE_KEY 时会绕过此策略）
-- 如果使用 ANON_KEY，此策略允许插入（user_id 可以为 NULL）
CREATE POLICY "Service can insert jobs"
  ON jobs FOR INSERT
  WITH CHECK (true);

-- 策略：用户只能创建自己的任务（如果提供了 user_id）
CREATE POLICY "Users can create their own jobs"
  ON jobs FOR INSERT
  WITH CHECK (auth.uid() = user_id OR user_id IS NULL);

-- 策略：允许后端服务更新任务
CREATE POLICY "Service can update jobs"
  ON jobs FOR UPDATE
  USING (true);

-- 策略：用户只能更新自己的任务
CREATE POLICY "Users can update their own jobs"
  ON jobs FOR UPDATE
  USING (auth.uid() = user_id);

-- 策略：公开任务（有 share_slug 的）可以被所有人查看
CREATE POLICY "Public jobs are viewable by everyone"
  ON jobs FOR SELECT
  USING (share_slug IS NOT NULL);

-- profiles 表 RLS
ALTER TABLE profiles ENABLE ROW LEVEL SECURITY;

-- 策略：用户只能查看和更新自己的 profile
CREATE POLICY "Users can view their own profile"
  ON profiles FOR SELECT
  USING (auth.uid() = id);

CREATE POLICY "Users can update their own profile"
  ON profiles FOR UPDATE
  USING (auth.uid() = id);

-- prompts_library 表 RLS（如果需要）
-- 如果模板库是公开的，可以允许所有人查看
ALTER TABLE prompts_library ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Prompts library is viewable by everyone"
  ON prompts_library FOR SELECT
  USING (true);

-- ============================================
-- 5. 函数：自动更新 updated_at 时间戳
-- ============================================
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- 为 jobs 表创建触发器
CREATE TRIGGER update_jobs_updated_at
  BEFORE UPDATE ON jobs
  FOR EACH ROW
  EXECUTE FUNCTION update_updated_at_column();

-- 为 profiles 表创建触发器
CREATE TRIGGER update_profiles_updated_at
  BEFORE UPDATE ON profiles
  FOR EACH ROW
  EXECUTE FUNCTION update_updated_at_column();

-- ============================================
-- 6. 函数：向量相似度搜索（用于模板推荐）
-- ============================================
-- 注意：此函数需要 pgvector 扩展
-- 如果不需要向量搜索功能，可以跳过此函数

CREATE OR REPLACE FUNCTION match_prompts(
  query_embedding vector(1536),
  match_count int DEFAULT 8,
  match_threshold float DEFAULT 0.5
)
RETURNS TABLE (
  id uuid,
  title text,
  category text,
  cover_url text,
  distance float
)
LANGUAGE sql
STABLE
AS $$
  SELECT
    p.id,
    p.title,
    p.category,
    p.cover_url,
    1 - (p.embedding <=> query_embedding) AS distance
  FROM prompts_library p
  WHERE p.embedding IS NOT NULL
    AND 1 - (p.embedding <=> query_embedding) > match_threshold
  ORDER BY p.embedding <=> query_embedding
  LIMIT match_count;
$$;

-- ============================================
-- 完成！
-- ============================================
-- 执行完成后，你可以：
-- 1. 在 Supabase Dashboard 的 "Table Editor" 中查看创建的表
-- 2. 测试 API 是否正常工作
-- 3. 如果需要，可以手动插入一些测试数据
-- ============================================

