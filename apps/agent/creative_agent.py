"""
创意策划 Agent - 负责通过多轮对话收集视频制作所需的信息
使用 CrewAI 的 planning、reasoning 和 HITL 功能
"""

from typing import Dict, Any, List, Optional
from crewai import Agent
from crewai.tools import tool
import json
import logging

logger = logging.getLogger("creative_agent")

# 视频类型选项
VIDEO_TYPES = [
    "产品宣传视频",
    "品牌故事视频",
    "教程视频",
    "活动推广视频",
    "社交媒体短视频",
    "广告片",
    "产品演示视频",
    "其他",
]

# 视频时长选项（秒）
DURATION_OPTIONS = [10, 20, 30, 60, 90, 120]

# 视频风格选项
STYLE_OPTIONS = [
    "现代简约",
    "科技感",
    "温馨生活",
    "时尚潮流",
    "商务专业",
    "创意艺术",
    "自然清新",
    "复古怀旧",
    "动感活力",
    "优雅高端",
]

# 一致性元素选项
CONSISTENCY_ELEMENTS = [
    "品牌Logo",
    "产品外观",
    "人物形象",
    "色彩方案",
    "字体样式",
    "包装设计",
    "用户界面",
    "场景背景",
]


@tool("收集视频制作信息")
def collect_video_info_tool(
    current_info: str,
    question_type: str,
    user_response: Optional[str] = None
) -> str:
    """
    收集视频制作所需的信息
    
    Args:
        current_info: 当前已收集的信息（JSON格式）
        question_type: 问题类型（video_type, duration, style, theme, keywords, key_elements, consistency_elements）
        user_response: 用户的回答（可选）
    
    Returns:
        JSON格式的更新后的信息
    """
    try:
        info = json.loads(current_info) if current_info else {}
    except:
        info = {}
    
    if user_response:
        if question_type == "video_type":
            info["video_type"] = user_response
        elif question_type == "duration":
            try:
                info["duration"] = float(user_response)
            except:
                info["duration"] = 10.0
        elif question_type == "style":
            if "styles" not in info:
                info["styles"] = []
            if user_response not in info["styles"]:
                info["styles"].append(user_response)
        elif question_type == "theme":
            info["theme"] = user_response
        elif question_type == "keywords":
            if "keywords" not in info:
                info["keywords"] = []
            keywords = [k.strip() for k in user_response.split(",") if k.strip()]
            info["keywords"].extend(keywords)
        elif question_type == "key_elements":
            if "key_elements" not in info:
                info["key_elements"] = []
            elements = [e.strip() for e in user_response.split(",") if e.strip()]
            info["key_elements"].extend(elements)
        elif question_type == "consistency_elements":
            if "consistency_elements" not in info:
                info["consistency_elements"] = []
            elements = [e.strip() for e in user_response.split(",") if e.strip()]
            info["consistency_elements"].extend(elements)
    
    return json.dumps(info, ensure_ascii=False)


def build_creative_agent_for_chat() -> Agent:
    """
    创建创意策划 Agent，负责通过对话收集信息和完善创意
    
    使用 planning 和 reasoning 功能来规划问题顺序和生成合适的问题
    使用 HITL 功能在需要时暂停等待用户输入
    
    参考: https://docs.crewai.com/en/concepts/agents
    - reasoning: 启用推理功能，让agent在提问前进行思考和规划
    - max_reasoning_attempts: 控制推理尝试次数
    - max_iter: 增加迭代次数以支持多轮对话
    """
    creative = Agent(
        role="创意策划",
        goal=(
            "通过多轮对话与用户沟通，识别用户意图，收集视频制作所需的所有信息，并完善视频创意。\n"
            "你需要收集以下信息：\n"
            "1. 视频类型（产品宣传、品牌故事、教程等）\n"
            "2. 视频时长（秒）\n"
            "3. 视频风格（现代简约、科技感等）\n"
            "4. 视频主题或核心内容\n"
            "5. 关键词（帮助理解核心信息）\n"
            "6. 关键元素（需要重点展示的元素）\n"
            "7. 一致性元素（需要在所有场景中保持一致的元素）\n"
            "根据用户的回答，智能地生成后续问题，并提供选项帮助用户快速选择。\n"
            "在收集完所有信息后，整理并完善视频创意，确保信息完整、清晰。"
        ),
        backstory=(
            "你是一位资深的创意策划，拥有10年以上的视频创意策划经验。\n"
            "你擅长与客户沟通，能够通过系统性的提问高效收集视频制作所需的信息。\n"
            "你能够识别用户的真实意图，理解用户的需求，并根据用户的回答智能地判断还需要收集哪些信息。\n"
            "你总是友好、专业，能够提供清晰的选项帮助用户快速做出选择。\n"
            "你的目标是高效、友好地收集完整信息，完善视频创意，并将整理后的完整信息传递给其他团队成员（导演、制片人等）。\n"
            "你负责创意策划阶段，不直接参与视频制作的技术细节。"
        ),
        tools=[collect_video_info_tool],
        verbose=True,
        allow_delegation=False,
        reasoning=True,  # 启用 reasoning，让 agent 在提问前进行思考和规划
        max_reasoning_attempts=5,  # 增加推理尝试次数，确保充分思考
        max_iter=50,  # 增加最大迭代次数，支持多轮对话
        max_execution_time=300,  # 最大执行时间5分钟
    )
    
    return creative


def generate_question_with_options(
    question_type: str,
    current_info: Dict[str, Any]
) -> tuple[str, List[str]]:
    """
    根据问题类型生成问题和选项
    
    Returns:
        (问题文本, 选项列表)
    """
    if question_type == "video_type":
        return (
            "首先，请告诉我您想要制作什么类型的视频？",
            VIDEO_TYPES
        )
    elif question_type == "duration":
        return (
            "好的，您希望视频的时长是多少秒？",
            [f"{d}秒" for d in DURATION_OPTIONS]
        )
    elif question_type == "style":
        return (
            "请选择视频的风格（可以选择多个）：",
            STYLE_OPTIONS
        )
    elif question_type == "theme":
        return (
            "请描述视频的主题或核心内容：",
            []
        )
    elif question_type == "keywords":
        return (
            "请提供一些关键词，这些关键词将帮助理解视频的核心信息（用逗号分隔）：",
            []
        )
    elif question_type == "key_elements":
        return (
            "请列出视频中需要重点展示的关键元素（用逗号分隔）：",
            []
        )
    elif question_type == "consistency_elements":
        return (
            "为了确保视频的一致性，请选择需要在所有场景中保持一致的元素（可以选择多个）：",
            CONSISTENCY_ELEMENTS
        )
    else:
        return ("", [])


def get_next_question(current_info: Dict[str, Any]) -> Optional[str]:
    """
    根据当前已收集的信息，决定下一个要问的问题
    
    Returns:
        下一个问题的类型，如果所有信息已收集完成则返回 None
    """
    if "video_type" not in current_info:
        return "video_type"
    if "duration" not in current_info:
        return "duration"
    if "styles" not in current_info or len(current_info.get("styles", [])) == 0:
        return "style"
    if "theme" not in current_info:
        return "theme"
    if "keywords" not in current_info or len(current_info.get("keywords", [])) == 0:
        return "keywords"
    if "key_elements" not in current_info or len(current_info.get("key_elements", [])) == 0:
        return "key_elements"
    if "consistency_elements" not in current_info or len(current_info.get("consistency_elements", [])) == 0:
        return "consistency_elements"
    
    return None  # 所有信息已收集完成

