from typing import Dict, Any, List
from crewai import Task, Crew, Process
from .crewai_agents import build_agents


def build_crew(payload: Dict[str, Any]) -> Crew:
    goal: str = payload.get("goal", "")
    styles: List[str] = payload.get("styles", []) or []
    total_duration: float = float(payload.get("total_duration", 6.0) or 6.0)
    num_clips: int = int(payload.get("num_clips", 1) or 1)
    image_control: bool = bool(payload.get("image_control", False))
    run_id: str = payload.get("run_id", "")
    
    # 从对话收集的信息
    keywords: List[str] = payload.get("keywords", []) or []
    key_elements: List[str] = payload.get("key_elements", []) or []
    consistency_elements: List[str] = payload.get("consistency_elements", []) or []
    video_type: str = payload.get("video_type", "")
    
    # 构建完整的目标描述，包含所有收集的信息
    goal_parts = [goal] if goal else []
    if video_type:
        goal_parts.append(f"视频类型：{video_type}")
    if keywords:
        goal_parts.append(f"关键词：{', '.join(keywords)}")
    if key_elements:
        goal_parts.append(f"关键元素：{', '.join(key_elements)}")
    if consistency_elements:
        goal_parts.append(f"一致性元素：{', '.join(consistency_elements)}")
    
    full_goal = " | ".join(goal_parts)
    
    agents = build_agents()
    # 根据角色名称选择核心角色
    def pick(role: str):
        for a in agents:
            if getattr(a, "role", "") == role:
                return a
        return None
    creative_agent = pick("创意策划")
    director_agent = pick("导演")
    reviewer_agent = pick("制片人")
    visual_agent = pick("美术")
    producer_agent = pick("视频剪辑师")
    editor_agent = producer_agent  # 使用 producer_agent 作为剪辑师
    
    # 创意策划任务 - 包含所有收集的信息
    creative_description = f"创意策划：根据用户输入和品牌调性进行创意构思和策略制定。\n"
    creative_description += f"目标：{full_goal}\n"
    if styles:
        creative_description += f"风格要求：{', '.join(styles)}\n"
    if total_duration:
        creative_description += f"视频时长：{total_duration}秒\n"
    if key_elements:
        creative_description += f"需要重点展示的元素：{', '.join(key_elements)}\n"
    if consistency_elements:
        creative_description += f"需要保持一致的元素：{', '.join(consistency_elements)}\n"
    
    task_optimize = Task(
        description=creative_description,
        agent=creative_agent,
        expected_output="优化后的创意策略和提示词文本",
    )
    
    # 规划分镜任务 - 包含所有收集的信息
    plan_description = f"规划分镜：\n"
    plan_description += f"目标：{full_goal}\n"
    if styles:
        plan_description += f"风格：{', '.join(styles)}\n"
    plan_description += f"时长：{total_duration}秒\n"
    if key_elements:
        plan_description += f"关键元素：{', '.join(key_elements)}\n"
    if consistency_elements:
        plan_description += f"一致性要求：{', '.join(consistency_elements)}必须在所有场景中保持一致\n"
    plan_description += f"【重要】输出格式要求：\n"
    plan_description += f"1. 必须使用 scene 结构，每个 scene 恰好10秒\n"
    plan_description += f"2. 每个 scene 必须包含完整的旁白文案（narration），约30-40字，覆盖整个10秒时长\n"
    plan_description += f"3. 每个 scene 包含多个镜头（clips），镜头数量可以根据内容需要灵活调整\n"
    plan_description += f"4. 每个镜头的时长（end_s - begin_s）必须不超过10s\n"
    plan_description += f"5. scene 内的镜头时间必须连续，且 scene 总时长必须恰好为10s\n"
    plan_description += f"6. 确保关键元素在适当的时候出现，一致性元素在所有场景中保持一致\n"
    plan_description += f"【关键】场景转场衔接要求：\n"
    plan_description += f"1. 每个场景的结尾画面应该自然过渡到下一个场景的开头画面\n"
    plan_description += f"2. 考虑场景之间的视觉连贯性，使用相似的色调、构图或元素进行衔接\n"
    plan_description += f"3. 相邻场景之间应该有逻辑关联，避免突兀的跳跃\n"
    plan_description += f"【关键】文案完整性要求：\n"
    plan_description += f"1. 每个场景的文案应该与画面内容完美匹配，描述场景中正在发生的事情\n"
    plan_description += f"2. 相邻场景的文案应该自然衔接，确保整体叙述的连贯性\n"
    # 使用单引号避免 f-string 中的反斜杠问题
    json_example = '{"scenes": [{"scene_idx": 1, "narration": "完整的旁白文案（30-40字）", "clips": [{"idx": 1, "desc": "...", "begin_s": 0.0, "end_s": 3.0}, ...], "begin_s": 0.0, "end_s": 10.0}, ...]}'
    plan_description += f"JSON 格式：{json_example}"
    
    task_plan = Task(
        description=plan_description,
        agent=director_agent,
        expected_output="分镜脚本（JSON 格式，包含 scenes 数组），每个 scene 恰好10秒，包含 narration（完整的旁白文案，30-40字），包含多个镜头（clips），每个镜头包含 idx, desc, begin_s, end_s",
        context=[task_optimize],
        # 注意：不在这里使用 human_input=True，因为我们在 main.py 中自己实现了 storyboard 确认流程
        # human_input 会在刷新页面后导致 CrewAI 等待用户输入，但前端无法处理
    )

    # 分镜脚本审核任务 - 包含一致性检查
    review_description = f"审核分镜脚本质量：\n"
    review_description += f"目标：{full_goal}\n"
    if styles:
        review_description += f"风格：{', '.join(styles)}\n"
    review_description += f"总时长：{total_duration}秒\n"
    if consistency_elements:
        review_description += f"一致性检查：确保以下元素在所有场景中保持一致：{', '.join(consistency_elements)}\n"
    review_description += f"检查每个 scene 的时长是否恰好为10s。\n"
    review_description += f"检查每个 scene 内的 clips 描述是否详细、具体（至少8个字符），不包含占位符。\n"
    review_description += f"确保每个镜头的时长不超过10s（end_s - begin_s <= 10s）。\n"
    review_description += f"确保 scene 内的 clips 时间连续且覆盖整个 scene 的时长。\n"
    review_description += f"如果审核未通过，工具会自动触发重写，最多重试3次，直到生成有效的分镜脚本。\n"
    review_description += f"如果审核通过，返回审核通过的分镜脚本（scene 结构）。"
    
    task_review = Task(
        description=review_description,
        agent=reviewer_agent,
        expected_output="审核通过的分镜脚本（JSON 格式，scene 结构），确保所有 scene 恰好10秒，所有 clips 都有详细描述且时长不超过10s",
        context=[task_plan],
    )
    
    # 合并镜头为视频任务（使用 scene 结构）
    task_merge = Task(
        description=(
            f"将分镜脚本转换为视频任务：run_id={run_id}, 总时长={total_duration}s\n"
            f"分镜脚本采用 scene 结构，每个 scene 恰好10秒，包含多个镜头（clips）。\n"
            f"规则：\n"
            f"1. 每个 scene 直接转换为一个视频任务\n"
            f"2. 每个视频任务对应一个 scene，时长恰好为10s\n"
            f"3. 使用 scene 的描述结构：合并 scene 内所有 clips 的描述（用'；'连接）\n"
            f"4. 保留 scene 的 clips 信息，以便后续使用\n"
            f"使用工具：合并镜头为视频任务工具（输入是包含 scenes 的 JSON）"
        ),
        agent=reviewer_agent,  # 使用制片人智能体（reviewer_agent 现在是制片人）
        expected_output="视频任务列表（JSON 格式），每个任务对应一个 scene，包含 task_idx, scene_idx, desc, clips, total_duration, begin_s, end_s，每个任务恰好10s",
        context=[task_review],
    )

    # 为每个 scene 生成图片（必须执行，用于前端展示）
    task_generate_scene_images = Task(
        description=(
            f"为每个 scene 生成预览图片：\n"
            f"输入是审核通过的分镜脚本（scene 结构）。\n"
            f"为每个 scene 生成一张代表性的预览图片，用于前端展示。\n"
            f"图片应该反映 scene 的整体视觉效果和主要内容。\n"
            f"【重要】生成的图片必须避免出现人物、人脸或真人形象，因为 sora2 不支持使用真人图片作为参考。\n"
            f"使用工具：生成关键帧工具（storyboards_json 参数传入 scene 结构的 JSON，image_control=True）\n"
            f"【重要】必须返回包含 image_url 字段的完整 scene 结构 JSON，格式：{{\"scenes\": [{{\"scene_idx\": 1, \"image_url\": \"...\", \"clips\": [...]}}, ...]}}"
        ),
        agent=visual_agent,
        expected_output="包含 scene 图片 URL 的分镜脚本（JSON 格式），每个 scene 包含 image_url 字段，格式：{\"scenes\": [{\"scene_idx\": 1, \"image_url\": \"...\", \"clips\": [...]}, ...]}",
        context=[task_review],  # 依赖审核任务
    )

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

    tasks = [t for t in [task_optimize, task_plan, task_review, task_generate_scene_images, task_merge, task_generate, task_stitch] if t]

    crew = Crew(
        agents=[a for a in [creative_agent, director_agent, reviewer_agent, visual_agent, producer_agent, editor_agent] if a],
        tasks=tasks,
        process=Process.sequential,  # 使用 sequential 模式，确保任务按顺序执行
        # 不使用 hierarchical 或 planning，因为我们的任务流程是固定的，sequential 更稳定
        verbose=True,
    )
    return crew


