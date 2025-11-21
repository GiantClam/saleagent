# CrewAI 多智能体编排方案

## 当前状态分析

### 现有实现
- **手动编排**：`/workflow/*` 接口通过函数调用链实现多步骤流程
- **伪 CrewAI**：`/crewai-agent` 端点仅模拟 Agent 事件流，未使用 CrewAI 框架
- **功能完整**：工作流已实现分镜规划、关键帧生成、视频生成、拼接等

### 问题
1. 未充分利用 CrewAI 的 Agent/Task 编排能力
2. 缺少 Agent 之间的协作与依赖管理
3. 难以扩展新的 Agent 角色
4. 缺少 CrewAI 的可观测性（日志、状态追踪）

## 设计方案

### Agent 角色定义

#### 1. PromptAgent（提示词优化 Agent）
- **角色**：资深广告导演
- **职责**：优化用户输入，生成可拍摄的镜头脚本
- **工具**：`optimize_prompt_tool`（调用 OpenRouter LLM）
- **输出**：优化后的提示词文本

#### 2. DirectorAgent（分镜导演 Agent）
- **角色**：分镜脚本导演
- **职责**：根据目标、风格、时长生成分镜脚本草案
- **工具**：`plan_storyboard_tool`（调用 OpenRouter LLM）
- **输出**：分镜脚本列表（ClipSpec[]）

#### 3. VisualAgent（视觉设计 Agent）
- **角色**：视觉设计师
- **职责**：为分镜生成关键帧（首帧/尾帧）
- **工具**：`generate_keyframe_tool`（调用 RunningHub 图片生成）
- **输出**：关键帧图片 URL

#### 4. ProducerAgent（制片人 Agent）
- **角色**：视频制片人
- **职责**：协调视频生成、并发控制、失败重试、最终拼接
- **工具**：
  - `generate_video_clip_tool`（调用 RunningHub Sora2）
  - `stitch_video_tool`（调用 FFmpeg 拼接）
- **输出**：最终视频 URL

### Task 任务定义

#### Task 1: 优化提示词
- **Agent**: PromptAgent
- **描述**: 将用户输入优化为可拍摄的镜头脚本
- **预期输出**: 优化后的提示词

#### Task 2: 规划分镜
- **Agent**: DirectorAgent
- **描述**: 根据目标、风格、时长生成分镜脚本
- **依赖**: Task 1（使用优化后的提示词）
- **预期输出**: 分镜脚本列表

#### Task 3: 生成关键帧（可选）
- **Agent**: VisualAgent
- **描述**: 为每个分镜生成首帧/尾帧图片
- **依赖**: Task 2（需要分镜脚本）
- **条件**: 用户选择 `image_control=true`
- **预期输出**: 关键帧图片 URL

#### Task 4: 生成视频片段
- **Agent**: ProducerAgent
- **描述**: 并发生成各分镜视频（最多4个）
- **依赖**: Task 2（必需）、Task 3（可选）
- **预期输出**: 视频片段 URL 列表

#### Task 5: 拼接最终视频
- **Agent**: ProducerAgent
- **描述**: 将所有视频片段拼接为最终视频
- **依赖**: Task 4
- **预期输出**: 最终视频 URL

### Crew 编排

```python
from crewai import Agent, Task, Crew, Process

# 创建 Agent
prompt_agent = Agent(
    role="资深广告导演",
    goal="优化用户输入为可拍摄的镜头脚本",
    backstory="你是一位经验丰富的广告导演，擅长将营销文案转化为视觉化的镜头语言。",
    tools=[optimize_prompt_tool],
    verbose=True
)

director_agent = Agent(
    role="分镜脚本导演",
    goal="根据目标、风格、时长生成分镜脚本",
    backstory="你是一位专业的分镜导演，擅长将创意转化为具体的拍摄计划。",
    tools=[plan_storyboard_tool],
    verbose=True
)

visual_agent = Agent(
    role="视觉设计师",
    goal="为分镜生成关键帧图片",
    backstory="你是一位视觉设计师，擅长为视频镜头设计关键画面。",
    tools=[generate_keyframe_tool],
    verbose=True
)

producer_agent = Agent(
    role="视频制片人",
    goal="协调视频生成、并发控制、最终拼接",
    backstory="你是一位经验丰富的视频制片人，擅长协调多个视频生成任务。",
    tools=[generate_video_clip_tool, stitch_video_tool],
    verbose=True
)

# 创建 Task
task_optimize = Task(
    description="优化提示词：{user_prompt}",
    agent=prompt_agent,
    expected_output="优化后的提示词文本"
)

task_plan = Task(
    description="规划分镜：目标={goal}, 风格={styles}, 时长={duration}s, 镜头数={num_clips}",
    agent=director_agent,
    expected_output="分镜脚本列表（JSON格式）"
)

task_keyframes = Task(
    description="生成关键帧：{storyboards}",
    agent=visual_agent,
    expected_output="关键帧图片URL列表",
    context=[task_plan]
)

task_generate_clips = Task(
    description="生成视频片段：{storyboards}",
    agent=producer_agent,
    expected_output="视频片段URL列表",
    context=[task_plan, task_keyframes]  # 条件依赖
)

task_stitch = Task(
    description="拼接最终视频：{clip_urls}",
    agent=producer_agent,
    expected_output="最终视频URL",
    context=[task_generate_clips]
)

# 创建 Crew
crew = Crew(
    agents=[prompt_agent, director_agent, visual_agent, producer_agent],
    tasks=[task_optimize, task_plan, task_keyframes, task_generate_clips, task_stitch],
    process=Process.sequential,  # 或 hierarchical（需要 manager）
    verbose=True
)
```

## 实施计划

### 阶段 1：工具封装（Tools）
1. 将现有 Provider 调用封装为 CrewAI Tools
2. 实现 `optimize_prompt_tool`、`plan_storyboard_tool`、`generate_keyframe_tool`、`generate_video_clip_tool`、`stitch_video_tool`

### 阶段 2：Agent 定义
1. 创建 4 个 Agent 实例
2. 配置角色、目标、背景故事
3. 绑定对应工具

### 阶段 3：Task 编排
1. 定义 5 个 Task
2. 设置依赖关系（context）
3. 配置条件执行（如关键帧生成）

### 阶段 4：Crew 执行
1. 创建 Crew 实例
2. 实现 SSE 事件流（与现有 `/crewai-agent` 兼容）
3. 集成到 `/workflow/*` 接口

### 阶段 5：可观测性
1. 集成 CrewAI 的日志系统
2. 输出 Agent 思考过程
3. 追踪 Task 执行状态

## 优势

1. **清晰的职责分离**：每个 Agent 专注单一职责
2. **可扩展性**：新增 Agent 只需定义新的 Agent 和 Task
3. **依赖管理**：CrewAI 自动管理 Task 之间的依赖
4. **可观测性**：CrewAI 提供详细的执行日志
5. **灵活性**：支持条件执行、并行执行等复杂场景

## 注意事项

1. **保持兼容性**：现有 `/workflow/*` 接口应继续工作
2. **渐进式迁移**：可以先实现 CrewAI 版本，与旧版本并行运行
3. **错误处理**：CrewAI 的 Task 失败需要妥善处理
4. **性能考虑**：CrewAI 可能增加一些开销，需要评估

## 下一步

1. 创建 `crewai_tools.py` 封装工具
2. 创建 `crewai_agents.py` 定义 Agent
3. 创建 `crewai_workflow.py` 实现 Crew 编排
4. 更新 `/workflow/*` 接口使用 CrewAI
5. 测试并优化性能

