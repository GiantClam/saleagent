-- ============================================
-- 快速修复 RLS 策略 - 允许后端服务插入和更新数据
-- ============================================
-- 使用方法：
-- 1. 登录 Supabase Dashboard
-- 2. 进入 SQL Editor
-- 3. 复制粘贴以下 SQL 并执行
-- ============================================

-- 删除旧的 INSERT 策略（如果存在）
DROP POLICY IF EXISTS "Users can create their own jobs" ON jobs;

-- 添加策略：允许服务端插入任务（使用 SERVICE_KEY 时会绕过此策略）
-- 如果使用 ANON_KEY，此策略允许插入（user_id 可以为 NULL）
CREATE POLICY "Service can insert jobs"
  ON jobs FOR INSERT
  WITH CHECK (true);

-- 添加策略：允许用户创建自己的任务（如果提供了 user_id）
CREATE POLICY "Users can create their own jobs"
  ON jobs FOR INSERT
  WITH CHECK (auth.uid() = user_id OR user_id IS NULL);

-- 删除旧的 UPDATE 策略（如果存在）
DROP POLICY IF EXISTS "Users can update their own jobs" ON jobs;

-- 添加策略：允许服务端更新任务
CREATE POLICY "Service can update jobs"
  ON jobs FOR UPDATE
  USING (true);

-- 添加策略：允许用户更新自己的任务
CREATE POLICY "Users can update their own jobs"
  ON jobs FOR UPDATE
  USING (auth.uid() = user_id);

-- ============================================
-- 完成！
-- ============================================
-- 执行后，后端应该可以正常插入和更新数据了
-- ============================================

