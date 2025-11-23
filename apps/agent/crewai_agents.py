from typing import List
from crewai import Agent
from crewai_tools import optimize_prompt_tool, plan_storyboard_tool, review_storyboard_tool, generate_keyframe_tool, merge_storyboards_to_video_tasks_tool, generate_video_clip_tool, stitch_video_tool  # type: ignore


def build_agents() -> List[Agent]:
    creative_agent = Agent(
        role="创意策划",
        goal="根据用户输入和品牌调性进行创意构思和策略制定。你负责接收来自创意策划对话阶段收集的完整信息，并基于这些信息完善创意策略。",
        backstory=(
            "你是一位资深的创意策划，擅长将用户需求转化为创意策略和视觉语言。"
            "你接收来自创意策划对话阶段收集的完整信息（包括视频类型、时长、风格、主题、关键词、关键元素、一致性元素等），"
            "并基于这些信息进行深入的创意构思和策略制定。"
            "你的工作是将用户的需求转化为可执行的创意方案，为后续的导演、制片人等团队成员提供清晰的创意指导。"
        ),
        tools=[optimize_prompt_tool],  # type: ignore[arg-type]
        verbose=True,
        allow_delegation=False,
        reasoning=True,  # 启用 reasoning，让 agent 在制定创意策略前进行思考
        max_reasoning_attempts=3,
    )

    director_agent = Agent(
        role="导演",
        goal="将创意转化为视觉语言，指导拍摄工作，确保影片的艺术质量",
        backstory="你是一位经验丰富的广告导演，擅长将创意转化为具体的拍摄计划。你将整个视频拆分为完整的可拍摄的镜头，每个分镜头最长不超过10s。",
        tools=[plan_storyboard_tool],  # type: ignore[arg-type]
        verbose=True,
        allow_delegation=False,
    )

    reviewer_agent = Agent(
        role="制片人",
        goal="审核镜头质量，按顺序将不足10s的镜头合并为10s的视频任务，创建视频片段素材拍摄任务",
        backstory="你是一位经验丰富的制片人，负责项目的整体规划、分镜头管理和进度控制。你确保每个镜头都有详细、具体的描述，并负责将镜头合并为10s的视频任务（这是出于拍摄成本考虑）。",
        tools=[review_storyboard_tool, merge_storyboards_to_video_tasks_tool],  # type: ignore[arg-type]
        verbose=True,
        allow_delegation=False,
    )

    visual_agent = Agent(
        role="美术",
        goal="负责场景布置、道具选择、人物一致性控制、指定物体一致性控制以及整体视觉风格的统一",
        backstory="你是一位专业的美术指导，擅长场景布置、道具选择，并确保人物和物体的一致性，保证整体视觉风格的统一。",
        tools=[generate_keyframe_tool],  # type: ignore[arg-type]
        verbose=True,
        allow_delegation=False,
    )

    producer_agent = Agent(
        role="视频制片人",
        goal="协调视频生成、并发控制、最终拼接",
        backstory="你是一位经验丰富的视频制片人，擅长协调多个视频生成任务。",
        tools=[generate_video_clip_tool, stitch_video_tool],  # type: ignore[arg-type]
        verbose=True,
        allow_delegation=False,
    )
    
    # editor_agent = Agent(
    #     role="剪辑师",
    #     goal="根据分镜头和导演的创意，将拍摄的素材进行剪辑、合并，构建影片的叙事结构",
    #     backstory="你是一位专业的视频剪辑师，擅长根据分镜头和导演的创意，将拍摄的素材进行剪辑、合并，构建影片的叙事结构。",
    #     tools=[stitch_video_tool],  # type: ignore[arg-type]
    #     verbose=True,
    #     allow_delegation=False,
    # )

    return [creative_agent, director_agent, reviewer_agent, visual_agent, producer_agent]


