from typing import Dict, Any, List
from crewai import Task, Crew, Process
from crewai_agents import build_agents


def build_crew(payload: Dict[str, Any]) -> Crew:
    goal: str = payload.get("goal", "")
    styles: List[str] = payload.get("styles", []) or []
    total_duration: float = float(payload.get("total_duration", 6.0) or 6.0)
    num_clips: int = int(payload.get("num_clips", 1) or 1)
    image_control: bool = bool(payload.get("image_control", False))
    run_id: str = payload.get("run_id", "")

    [creative_agent, director_agent, reviewer_agent, visual_agent, producer_agent, editor_agent] = build_agents()

    task_optimize = Task(
        description=f"创意策划：根据用户输入和品牌调性进行创意构思和策略制定。目标：{goal}",
        agent=creative_agent,
        expected_output="优化后的创意策略和提示词文本",
    )

    task_plan = Task(
        description=(
            f"规划分镜：目标={goal}, 风格={','.join(styles)}, 时长={total_duration}s\n"
            f"【重要】每个分镜头最长不超过10s，按时间顺序规划。"
            f"镜头数量可以根据需要灵活调整，但每个镜头时长不能超过10s。"
        ),
        agent=director_agent,
        expected_output="分镜脚本列表（JSON 格式），每个镜头包含 idx, desc, begin_s, end_s，且 end_s - begin_s <= 10s",
        context=[task_optimize],
    )

    # 分镜脚本审核任务
    task_review = Task(
        description=(
            f"审核分镜脚本质量：目标={goal}, 风格={','.join(styles)}, 总时长={total_duration}s\n"
            f"检查每个镜头的描述是否详细、具体（至少8个字符），不包含占位符。\n"
            f"确保每个镜头的时长不超过10s（end_s - begin_s <= 10s）。\n"
            f"如果审核未通过，工具会自动触发重写，最多重试3次，直到生成有效的分镜脚本。\n"
            f"如果审核通过，返回审核通过的分镜脚本。"
        ),
        agent=reviewer_agent,
        expected_output="审核通过的分镜脚本（JSON 格式），确保所有镜头都有详细描述且时长不超过10s",
        context=[task_plan],
    )
    
    # 合并镜头为视频任务（新增）
    task_merge = Task(
        description=(
            f"合并镜头为视频任务：run_id={run_id}, 总时长={total_duration}s\n"
            f"将审核通过的分镜脚本按时间顺序合并为10s的视频任务。\n"
            f"规则：\n"
            f"1. 按时间顺序（begin_s）处理镜头\n"
            f"2. 将不足10s的镜头合并，直到累计时长达到10s\n"
            f"3. 每个视频任务最多10s，如果单个镜头超过10s，则单独成为一个任务\n"
            f"4. 合并时，将多个镜头的描述用'；'连接\n"
            f"这是为了节约成本，按照10s一个视频素材进行生成。\n"
            f"使用工具：合并镜头为视频任务工具"
        ),
        agent=reviewer_agent,  # 使用制片人智能体（reviewer_agent 现在是制片人）
        expected_output="视频任务列表（JSON 格式），每个任务包含 task_idx, desc, total_duration, begin_s, end_s，每个任务对应一个10s的视频片段",
        context=[task_review],
    )

    # 关键帧任务（可选）
    keyframe_description = "生成关键帧：{storyboards_json}"
    if image_control:
        task_keyframes = Task(
            description=keyframe_description,
            agent=visual_agent,
            expected_output="更新后的分镜脚本（含关键帧，JSON）",
            context=[task_review],  # 依赖审核任务
        )
    else:
        task_keyframes = None

    task_generate = Task(
        description=(
            f"提交视频片段生成任务：run_id={run_id}\n"
            f"注意：视频生成需要 3-5 分钟，此任务只负责提交异步任务到后台。\n"
            f"如果任务返回 pending 状态，说明已成功提交，实际生成由 webhook 或后台轮询完成。\n"
            f"使用工具：生成视频片段工具（输入是合并后的视频任务列表，不是原始分镜脚本）"
        ),
        agent=producer_agent,
        expected_output="视频片段任务提交结果列表（JSON，包含 task_idx, task_id 和 status）",
        context=[task_merge],  # 依赖合并任务
    )

    task_stitch = Task(
        description=(
            f"剪辑合并最终视频：run_id={run_id}\n"
            f"根据分镜头和导演的创意，将拍摄的素材进行剪辑、合并，构建影片的叙事结构。\n"
            f"【重要规则】\n"
            f"1. 如果视频片段状态为 pending 或 submitted，说明任务还在处理中，此时无法拼接。\n"
            f"2. 必须等待所有视频片段状态为 succeeded 后才能进行拼接。\n"
            f"3. 如果工具返回错误信息（包含'pending'、'处理中'、'无法拼接'等），说明视频还在生成，"
            f"   此时必须如实返回错误信息，绝对不要自己生成或猜测视频 URL。\n"
            f"4. 禁止生成占位符 URL（如 cdn.example.com 或任何 example.com 域名）。\n"
            f"5. 只有在工具成功返回视频 URL 时，才能使用该 URL。\n"
            f"6. 如果工具抛出异常，必须如实返回异常信息，不要自己编造 URL。"
        ),
        agent=editor_agent,
        expected_output="最终视频的 CDN URL（仅当所有视频片段完成时），或者错误信息（如果有 pending 任务）。禁止生成占位符 URL。",
        context=[task_generate],
    )

    tasks = [t for t in [task_optimize, task_plan, task_review, task_keyframes, task_merge, task_generate, task_stitch] if t]

    crew = Crew(
        agents=[creative_agent, director_agent, reviewer_agent, visual_agent, producer_agent, editor_agent],
        tasks=tasks,
        process=Process.sequential,
        verbose=True,
    )
    return crew


