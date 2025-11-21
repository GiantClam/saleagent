# Supabase 环境变量配置指南

## 快速开始

### 第一步：创建数据库表

**重要**：在配置环境变量之前，需要先在 Supabase 中创建数据库表。

1. 登录 [Supabase Dashboard](https://app.supabase.com)
2. 进入你的项目
3. 点击左侧菜单 **SQL Editor**
4. 点击 **New query**
5. 打开项目根目录的 `supabase_schema.sql` 文件
6. 复制全部内容并粘贴到 SQL Editor
7. 点击 **Run** 执行

或者，你也可以直接复制以下**最小必需表结构**（仅创建 `jobs` 表）：

```sql
CREATE TABLE IF NOT EXISTS jobs (
  run_id TEXT PRIMARY KEY,
  user_id UUID,
  slogan TEXT,
  cover_url TEXT,
  video_url TEXT,
  share_slug TEXT UNIQUE,
  status TEXT DEFAULT 'running',
  provider_task_id TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_jobs_user_id ON jobs(user_id);
CREATE INDEX IF NOT EXISTS idx_jobs_share_slug ON jobs(share_slug);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
```

### 第二步：配置环境变量

完成建表后，继续下面的环境变量配置。

---

## 需要的环境变量

### 后端（apps/agent/.env）

```bash
# Supabase 项目 URL
SUPABASE_URL=https://xxxxx.supabase.co

# Supabase API Key（二选一，推荐使用 SERVICE_KEY）
# 选项 1：服务密钥（Service Role Key）- 推荐用于后端，可绕过 RLS
SUPABASE_SERVICE_KEY=eyJ...  # 从 Supabase Dashboard 获取

# 选项 2：匿名密钥（Anon Key）- 如果使用此选项，需要调整 RLS 策略
# SUPABASE_ANON_KEY=eyJ...  # 从 Supabase Dashboard 获取
```

**重要提示**：
- **推荐使用 `SUPABASE_SERVICE_KEY`**：后端需要插入和更新数据，使用 Service Key 可以绕过 RLS 限制
- 如果使用 `SUPABASE_ANON_KEY`，需要确保 RLS 策略允许后端操作（`supabase_schema.sql` 已包含相应策略）

### 前端（apps/web/.env.local）

```bash
# Supabase 项目 URL（必须以 NEXT_PUBLIC_ 开头，才能在客户端使用）
NEXT_PUBLIC_SUPABASE_URL=https://xxxxx.supabase.co

# Supabase 匿名密钥（必须以 NEXT_PUBLIC_ 开头）
NEXT_PUBLIC_SUPABASE_ANON_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
```

## 如何获取这些值

### 步骤 1：创建 Supabase 项目

1. 访问 [https://supabase.com](https://supabase.com)
2. 注册/登录账号
3. 点击 "New Project" 创建新项目
4. 填写项目信息：
   - **Name**: 项目名称（如：saleagent）
   - **Database Password**: 设置数据库密码（请妥善保存）
   - **Region**: 选择离你最近的区域
5. 等待项目创建完成（约 2-3 分钟）

### 步骤 2：获取项目 URL 和 API Keys

1. 在 Supabase Dashboard 中，进入你的项目
2. 点击左侧菜单的 **Settings**（设置）
3. 点击 **API** 子菜单
4. 在 **Project URL** 部分，复制 **Project URL**：
   ```
   https://xxxxx.supabase.co
   ```
   这就是 `SUPABASE_URL` 和 `NEXT_PUBLIC_SUPABASE_URL` 的值

5. 在 **Project API keys** 部分，你会看到两个密钥：
   - **anon public** - 这是匿名密钥（Anon Key）
     - 用于：前端客户端和大部分后端操作
     - 特点：受 Row Level Security (RLS) 保护
     - 这就是 `SUPABASE_ANON_KEY` 和 `NEXT_PUBLIC_SUPABASE_ANON_KEY` 的值
   
   - **service_role secret** - 这是服务密钥（Service Role Key）
     - 用于：需要绕过 RLS 的后端操作
     - 特点：拥有完整权限，**不要在前端使用**
     - 这就是 `SUPABASE_SERVICE_KEY` 的值（可选）

### 步骤 3：配置环境变量

#### 后端配置（apps/agent/.env）

```bash
# 必需
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_ANON_KEY=eyJ...  # 从 Supabase Dashboard 获取

# 可选：如果需要绕过 RLS 的操作
# SUPABASE_SERVICE_KEY=eyJ...  # 从 Supabase Dashboard 获取
```

#### 前端配置（apps/web/.env.local）

```bash
# 必需（注意：必须以 NEXT_PUBLIC_ 开头）
NEXT_PUBLIC_SUPABASE_URL=https://your-project.supabase.co
NEXT_PUBLIC_SUPABASE_ANON_KEY=eyJ...  # 从 Supabase Dashboard 获取
```

## 项目中的使用场景

### 后端使用（apps/agent/main.py）

- **存储任务数据**：`jobs` 表存储视频生成任务
- **用户管理**：查询用户信息（`users`、`profiles` 表）
- **模板库**：`prompts_library` 表存储营销模板
- **向量检索**：使用 pgvector 进行相似度搜索（可选）

### 前端使用（apps/web）

- **用户认证**：Google/GitHub OAuth 登录
- **用户信息**：获取当前登录用户信息
- **任务列表**：显示用户的任务历史

## 数据库表结构

项目需要以下 Supabase 表：

### 1. jobs 表

```sql
CREATE TABLE IF NOT EXISTS jobs (
  run_id TEXT PRIMARY KEY,
  user_id UUID,
  slogan TEXT,
  cover_url TEXT,
  video_url TEXT,
  share_slug TEXT UNIQUE,
  status TEXT DEFAULT 'running',
  provider_task_id TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 创建索引
CREATE INDEX IF NOT EXISTS idx_jobs_user_id ON jobs(user_id);
CREATE INDEX IF NOT EXISTS idx_jobs_share_slug ON jobs(share_slug);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
```

### 2. prompts_library 表（可选，用于模板推荐）

```sql
-- 首先启用 pgvector 扩展
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS prompts_library (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  category TEXT,
  title TEXT UNIQUE,
  prompt TEXT,
  cover_url TEXT,
  embedding vector(1536),  -- 向量维度根据使用的模型调整
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 创建向量索引（可选，提升检索性能）
CREATE INDEX IF NOT EXISTS idx_prompts_embedding ON prompts_library 
USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
```

### 3. users 表（Supabase Auth 自动管理）

Supabase Auth 会自动创建 `auth.users` 表，无需手动创建。

### 4. profiles 表（可选，扩展用户信息）

```sql
CREATE TABLE IF NOT EXISTS profiles (
  id UUID PRIMARY KEY REFERENCES auth.users(id),
  email TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);
```

## 安全建议

1. **前端使用 Anon Key**：前端必须使用 `NEXT_PUBLIC_SUPABASE_ANON_KEY`（匿名密钥）
2. **后端优先使用 Anon Key**：大部分情况下后端也使用 Anon Key，配合 RLS 保护数据
3. **Service Key 谨慎使用**：只在确实需要绕过 RLS 时使用 Service Key
4. **启用 Row Level Security (RLS)**：在 Supabase Dashboard 中为表启用 RLS 策略

## 验证配置

### 后端验证

```bash
cd apps/agent
python -c "
from dotenv import load_dotenv
import os
load_dotenv()
from supabase import create_client

url = os.getenv('SUPABASE_URL')
key = os.getenv('SUPABASE_ANON_KEY') or os.getenv('SUPABASE_SERVICE_KEY')

if url and key:
    client = create_client(url, key)
    print('✅ Supabase 连接成功')
    # 测试查询
    try:
        result = client.table('jobs').select('run_id').limit(1).execute()
        print('✅ 数据库查询成功')
    except Exception as e:
        print(f'⚠️  数据库查询失败: {e}')
        print('   提示：可能需要先创建 jobs 表')
else:
    print('❌ 环境变量未配置')
"
```

### 前端验证

在浏览器控制台运行：

```javascript
// 检查环境变量是否加载
console.log('SUPABASE_URL:', process.env.NEXT_PUBLIC_SUPABASE_URL);
console.log('SUPABASE_KEY:', process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY?.substring(0, 20) + '...');
```

## 常见问题

### Q: Anon Key 和 Service Key 有什么区别？

- **Anon Key**：受 RLS 保护，只能访问用户有权限的数据
- **Service Key**：绕过 RLS，拥有完整权限，适合后端管理操作

### Q: 前端为什么必须用 NEXT_PUBLIC_ 前缀？

Next.js 只将 `NEXT_PUBLIC_` 开头的环境变量暴露给客户端代码，这是安全机制。

### Q: 如何启用 OAuth 登录（Google/GitHub）？

1. 在 Supabase Dashboard 中，进入 **Authentication** > **Providers**
2. 启用 **Google** 或 **GitHub**
3. 配置 OAuth 应用的 Client ID 和 Secret
4. 设置 Redirect URL：`https://your-domain.com`

### Q: 如何设置 Row Level Security (RLS)？

1. 在 Supabase Dashboard 中，进入 **Table Editor**
2. 选择表（如 `jobs`）
3. 点击 **RLS** 标签
4. 启用 RLS
5. 创建策略（Policies）定义访问规则

## 参考链接

- [Supabase 官方文档](https://supabase.com/docs)
- [Supabase Auth 文档](https://supabase.com/docs/guides/auth)
- [Row Level Security 指南](https://supabase.com/docs/guides/auth/row-level-security)
- [pgvector 向量搜索](https://supabase.com/docs/guides/ai/vector-columns)

