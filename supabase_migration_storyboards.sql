-- ============================================
-- 迁移脚本：为 jobs 表添加分镜相关字段
-- ============================================
-- 使用方法：
-- 1. 登录 Supabase Dashboard
-- 2. 进入 SQL Editor
-- 3. 执行此脚本
-- ============================================

-- 添加 storyboards JSONB 字段
ALTER TABLE jobs 
ADD COLUMN IF NOT EXISTS storyboards JSONB;

-- 添加 total_duration 字段
ALTER TABLE jobs 
ADD COLUMN IF NOT EXISTS total_duration REAL;

-- 添加 styles 数组字段
ALTER TABLE jobs 
ADD COLUMN IF NOT EXISTS styles TEXT[];

-- 添加 image_control 布尔字段
ALTER TABLE jobs 
ADD COLUMN IF NOT EXISTS image_control BOOLEAN DEFAULT FALSE;

-- 创建索引以提升 JSONB 查询性能（可选）
CREATE INDEX IF NOT EXISTS idx_jobs_storyboards ON jobs USING GIN (storyboards);

-- ============================================
-- 完成！
-- ============================================

