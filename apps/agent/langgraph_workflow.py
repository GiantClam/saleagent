import os
import json
import asyncio
import logging
from typing import TypedDict, List, Dict, Any, Annotated, Optional, Union
import operator
from datetime import datetime

from langgraph.graph import StateGraph, END
from langchain_openai import ChatOpenAI
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, SystemMessage

# Import existing tools and logic
from crewai_tools import (
    plan_storyboard_impl, 
    generate_video_clip_impl,
    merge_storyboards_to_video_tasks_impl
)
from providers import get_image_provider, get_video_provider
from r2 import upload_url_to_r2
from job_manager import job_manager

logger = logging.getLogger("langgraph_workflow")

# --- State Definition ---

class AgentState(TypedDict):
    # Input / Context
    goal: str
    styles: List[str]
    total_duration: float
    num_clips: int
    image_control: bool
    run_id: str
    thread_id: str
    
    # Internal Data
    storyboard: Dict[str, Any]
    video_tasks: List[Dict[str, Any]]
    clip_results: List[Dict[str, Any]]
    
    # [NEW] Information Gathering State
    collected_info: Dict[str, Any]
    next_question: Optional[str]
    options: List[str]
    
    # [NEW] Chat History for context
    messages: List[BaseMessage]
    
    # Output
    final_video_url: str
    final_audio_url: Optional[str]
    
    # Control Flags / Status
    use_avatar: bool
    status: str # 'gathering', 'planning', 'awaiting_approval', 'generating', 'processing', 'completed', 'failed'
    error: Optional[str]
    loop_count: int

# --- Nodes Implementation ---

from creative_agent import get_next_question, generate_question_with_options, collect_video_info_tool

async def collector_node(state: AgentState):
    """
    Handles information gathering through multi-turn conversation.
    """
    logger.info(f"[collector_node] Checking info for run_id={state['run_id']}")
    
    collected_info = state.get("collected_info", {})
    last_message = state["messages"][-1].content if state["messages"] else ""
    
    # If there's a user response and a pending question type, update info
    current_q_type = state.get("next_question")
    if current_q_type and last_message:
        info_json = collect_video_info_tool.fn(
            current_info=json.dumps(collected_info),
            question_type=current_q_type,
            user_response=last_message
        )
        collected_info = json.loads(info_json)
    
    # Determine the next question
    next_q_type = get_next_question(collected_info)
    
    if next_q_type:
        question_text, options = generate_question_with_options(next_q_type, collected_info)
        return {
            "collected_info": collected_info,
            "next_question": next_q_type,
            "options": options,
            "status": "gathering",
            "messages": [AIMessage(content=question_text)]
        }
    else:
        # All info gathered
        return {
            "collected_info": collected_info,
            "next_question": None,
            "options": [],
            "status": "planning"
        }

async def planner_node(state: AgentState):
    """
    Equivalent to CrewAI's Optimize + Plan + Review tasks.
    Generates the initial storyboard JSON.
    """
    logger.info(f"[planner_node] Running for run_id={state['run_id']}")
    
    try:
        # Re-using the logic from crewai_tools with enhanced input
        storyboard_json = await plan_storyboard_impl(
            goal=state['goal'],
            styles=state['styles'],
            total_duration=state['total_duration'],
            num_clips=state['num_clips'],
            run_id=state['run_id'],
            collected_info=state.get('collected_info')
        )
        
        storyboard = json.loads(storyboard_json)
        
        return {
            "storyboard": storyboard,
            "status": "awaiting_approval",
            "loop_count": state.get("loop_count", 0) + 1
        }
    except Exception as e:
        logger.error(f"Planner node failed: {e}")
        return {"error": str(e), "status": "failed"}

async def image_gen_node(state: AgentState):
    """
    Generates preview images for each scene if image_control is enabled.
    """
    if not state.get("image_control"):
        return state
    
    logger.info(f"[image_gen_node] Generating scene images for run_id={state['run_id']}")
    storyboard = state["storyboard"]
    ip = get_image_provider()
    
    for scene in storyboard.get("scenes", []):
        scene_desc = scene.get("narration") or scene.get("desc", "")
        if scene_desc and not scene.get("keyframes", {}).get("in"):
            try:
                img_url = await ip.generate_scene(scene_desc)
                if not scene.get("keyframes"):
                    scene["keyframes"] = {}
                scene["keyframes"]["in"] = img_url
            except Exception as e:
                logger.warning(f"Image generation failed for scene {scene.get('scene_idx')}: {e}")
                
    return {"storyboard": storyboard}

async def updater_node(state: AgentState):
    """
    Handles updates from the editor.
    Updates the storyboard and determines what needs to be regenerated.
    """
    logger.info(f"[updater_node] Updating run_id={state['run_id']}")
    
    # The new state is already merged by the time we get here if using update_state
    # We just need to ensure the status is reset to allow re-entry into gen nodes
    return {"status": "planning"}

async def task_prepper_node(state: AgentState):
    """
    Merges storyboard scenes into video generation tasks.
    """
    logger.info(f"[task_prepper_node] Preparing video tasks for run_id={state['run_id']}")
    storyboard_json = json.dumps(state['storyboard'])
    
    video_tasks_json = merge_storyboards_to_video_tasks_impl(
        storyboard_json, 
        state['run_id'], 
        state['total_duration']
    )
    
    video_tasks = json.loads(video_tasks_json) if isinstance(video_tasks_json, str) else video_tasks_json
    return {"video_tasks": video_tasks, "status": "generating"}

async def executor_node(state: AgentState):
    """
    Submits video generation tasks to the provider using the Unified Generator Layer.
    """
    logger.info(f"[executor_node] Submitting video tasks for run_id={state['run_id']}")
    
    provider = get_video_provider()
    video_tasks = state.get("video_tasks", [])
    clip_results = []
    
    from supabase import create_client
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_SERVICE_KEY")
    supabase_client = create_client(supabase_url, supabase_key)

    for task in video_tasks:
        task_idx = task.get("task_idx") or task.get("idx")
        logger.info(f"[executor_node] Submitting task {task_idx}...")
        
        # Prepare generator arguments
        gen_kwargs = {
            "prompt": task.get("prompt"),
            "image_url": task.get("image_url") or task.get("ref_img"),
            "duration": task.get("duration", 10)
        }
        
        # Use MultiGenerator (Unified Layer)
        result = await provider.generate(**gen_kwargs)
        
        if result.get("status") == "failed":
            logger.error(f"[executor_node] Task {task_idx} failed submission: {result.get('error')}")
            clip_results.append({
                "task_idx": task_idx,
                "status": "failed",
                "error": result.get("error")
            })
        else:
            # Pending or success
            task_id = result.get("task_id")
            logger.info(f"[executor_node] Task {task_idx} submitted. Provider SID: {task_id}")
            
            # Record in DB for polling
            task_data = {
                "run_id": state['run_id'],
                "clip_idx": task_idx,
                "prompt": task.get("prompt"),
                "ref_img": task.get("image_url") or task.get("ref_img") or "",
                "duration": task.get("duration", 10),
                "status": "submitted",
                "provider_task_id": task_id,
                "task_metadata": json.dumps(result) # Store metadata (like _generator_index)
            }
            supabase_client.table("video_tasks").insert(task_data).execute()
            
            clip_results.append({
                "task_idx": task_idx,
                "status": "submitted",
                "task_id": task_id,
                "task_metadata": result
            })
            
    return {"clip_results": clip_results, "status": "processing"}

async def poller_node(state: AgentState):
    """
    Check the status of submitted video tasks using the Unified Generator Layer.
    """
    logger.info(f"[poller_node] Checking task status for run_id={state['run_id']}")
    
    from supabase import create_client
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_SERVICE_KEY")
    supabase_client = create_client(supabase_url, supabase_key)
    
    provider = get_video_provider()
    
    try:
        # Get tasks from DB to check their current status
        res = supabase_client.table("video_tasks").select("*").eq("run_id", state['run_id']).execute()
        db_tasks = res.data or []
        
        updated_tasks = []
        all_done = True
        any_failed = False
        
        for task in db_tasks:
            if task.get("status") in ("succeeded", "failed"):
                updated_tasks.append(task)
                if task.get("status") == "failed": any_failed = True
                continue
                
            # If still pending/submitted, check via provider
            # task_metadata contains the sid and _generator_index
            metadata_str = task.get("task_metadata")
            if not metadata_str:
                all_done = False
                continue
                
            metadata = json.loads(metadata_str)
            status_res = await provider.get_status(metadata)
            
            if status_res.get("status") == "success":
                # Update DB
                video_url = status_res.get("url")
                supabase_client.table("video_tasks").update({
                    "status": "succeeded",
                    "video_url": video_url,
                    "updated_at": datetime.utcnow().isoformat()
                }).eq("id", task["id"]).execute()
                
                task["status"] = "succeeded"
                task["video_url"] = video_url
            elif status_res.get("status") == "failed":
                error = status_res.get("error", "Unknown provider error")
                supabase_client.table("video_tasks").update({
                    "status": "failed",
                    "error": error,
                    "updated_at": datetime.utcnow().isoformat()
                }).eq("id", task["id"]).execute()
                
                task["status"] = "failed"
                task["error"] = error
                any_failed = True
            else:
                # Still pending
                all_done = False
            
            updated_tasks.append(task)
            
        # Broadcast progress
        completed_count = sum(1 for t in updated_tasks if t.get("status") == "succeeded")
        total_count = len(state.get("video_tasks", []))
        if total_count > 0:
            await job_manager.broadcast(
                state['run_id'], "制片", "progress",
                f"生成进度：{completed_count}/{total_count}",
                {"current": completed_count, "total": total_count}
            )

        if all_done and not any_failed:
            return {"status": "ready_to_stitch", "clip_results": updated_tasks}
        elif any_failed:
            return {"status": "failed", "error": "One or more clips failed generation"}
        else:
            # Still processing
            await asyncio.sleep(5) 
            return {"status": "processing"}
            
    except Exception as e:
        logger.error(f"Polling check failed: {e}")
        return {"error": str(e), "status": "failed"}

async def stitcher_node(state: AgentState):
    """
    Stitches the completed clips into a final video.
    """
    logger.info(f"[stitcher_node] Stitching final video for run_id={state['run_id']}")
    from video_stitcher import stitch_videos_for_run
    
    try:
        final_url = await stitch_videos_for_run(state['run_id'])
        # For now, we reuse the video URL for audio context if separate audio isn't available
        return {"final_video_url": final_url, "final_audio_url": final_url, "status": "completed"}
    except Exception as e:
        logger.error(f"Stitcher node failed: {e}")
        return {"error": str(e), "status": "failed"}

from avatar_agent import avatar_node

# --- Graph Orchestration ---

from langgraph.checkpoint.memory import MemorySaver

def build_video_graph():
    # Persistence for human-in-the-loop and state recovery
    checkpointer = MemorySaver()
    
    workflow = StateGraph(AgentState)
    
    # Add Nodes
    workflow.add_node("collector", collector_node)
    workflow.add_node("planner", planner_node)
    workflow.add_node("updater", updater_node)
    workflow.add_node("image_gen", image_gen_node)
    workflow.add_node("task_prepper", task_prepper_node)
    workflow.add_node("executor", executor_node)
    workflow.add_node("poller", poller_node)
    workflow.add_node("stitcher", stitcher_node)
    workflow.add_node("avatar", avatar_node)
    
    # Set Entry Point
    workflow.set_entry_point("collector")
    
    # Define Edges
    def check_gathering_status(state: AgentState):
        if state.get("status") == "gathering":
            return "ask"
        else:
            return "plan"

    workflow.add_conditional_edges(
        "collector",
        check_gathering_status,
        {
            "ask": END,
            "plan": "planner"
        }
    )

    workflow.add_edge("planner", "image_gen")
    
    # Define a breakpoint after image generation to wait for user approval
    workflow.add_edge("image_gen", "task_prepper")
    
    workflow.add_edge("task_prepper", "executor")
    workflow.add_edge("executor", "poller")
    
    # Conditional edge for Polling
    def check_poll_status(state: AgentState):
        status = state.get("status")
        if status == "ready_to_stitch":
            return "stitch"
        elif status == "failed":
            return "fail"
        else:
            return "wait"
        
    workflow.add_conditional_edges(
        "poller",
        check_poll_status,
        {
            "stitch": "stitcher",
            "wait": "poller",
            "fail": END
        }
    )
    
    workflow.add_edge("stitcher", "avatar")
    workflow.add_edge("avatar", END)
    
    # Compile with persistence and interrupt
    # The graph will stop after 'image_gen' and wait for user input (resume)
    return workflow.compile(
        checkpointer=checkpointer,
        interrupt_after=["image_gen"]
    )

# --- Integration Helpers ---

_app = None

def get_workflow_app():
    global _app
    if _app is None:
        _app = build_video_graph()
    return _app

async def start_video_generation(payload: Dict[str, Any]):
    """
    Starts or resumes the video generation workflow.
    """
    app = get_workflow_app()
    run_id = payload.get("run_id")
    thread_id = payload.get("thread_id") or f"thread_{run_id}"
    
    config = {"configurable": {"thread_id": thread_id}}
    
    # Check if we should resume or start new
    state = await app.aget_state(config)
    
    if state.values:
        # Resume (User approved the plan)
        logger.info(f"Resuming workflow for {run_id}")
        return await app.ainvoke(None, config)
    else:
        # Start fresh
        logger.info(f"Starting new workflow for {run_id}")
        inputs = {
            "goal": payload.get("goal"),
            "styles": payload.get("styles", []),
            "total_duration": payload.get("total_duration", 10.0),
            "num_clips": payload.get("num_clips", 0),
            "image_control": payload.get("image_control", False),
            "use_avatar": payload.get("use_avatar", False),
            "run_id": run_id,
            "thread_id": thread_id,
            "loop_count": 0,
            "collected_info": {},
            "messages": [HumanMessage(content=payload.get("goal") or "")]
        }
        return await app.ainvoke(inputs, config)

async def update_video_generation(run_id: str, thread_id: str, updates: Dict[str, Any]):
    """
    Updates the state of an existing workflow and triggers the updater node.
    """
    app = get_workflow_app()
    config = {"configurable": {"thread_id": thread_id}}
    
    # Update the state (e.g., modified storyboard)
    await app.aupdate_state(config, updates, as_node="updater")
    
    # Trigger the workflow to continue from updater
    return await app.ainvoke(None, config)
