import os
import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional

_manager = None


class SessionManager:
    def __init__(self, supabase_client) -> None:
        self.supabase = supabase_client

    async def register_session(self, run_id: str, session_id: str, expected_clips: int, context: Optional[Dict[str, Any]] = None) -> None:
        if not self.supabase:
            return
        ctx = context or {}
        self.supabase.table("crew_sessions")\
            .upsert({
                "run_id": run_id,
                "session_id": session_id,
                "expected_clips": int(expected_clips or 0),
                "status": ctx.get("status") or "running",
                "context": ctx,
                "updated_at": datetime.utcnow().isoformat()
            }, on_conflict="run_id")\
            .execute()

    async def check_and_trigger_stitch(self, run_id: str) -> None:
        if not self.supabase:
            return
        sess = self.supabase.table("crew_sessions")\
            .select("expected_clips,status")\
            .eq("run_id", run_id)\
            .maybe_single()\
            .execute()
        expected = int((sess.data or {}).get("expected_clips") or 0)
        status = str((sess.data or {}).get("status") or "")
        if status in {"stitching", "completed"}:
            return
        tasks = self.supabase.table("video_tasks")\
            .select("clip_idx,video_url,status")\
            .eq("run_id", run_id)\
            .execute()
        items = tasks.data or []
        completed = [t for t in items if str(t.get("status") or "") == "succeeded" and t.get("video_url")]
        if expected and len(completed) >= expected:
            self.supabase.table("crew_sessions")\
                .update({
                    "status": "stitching",
                    "updated_at": datetime.utcnow().isoformat()
                })\
                .eq("run_id", run_id)\
                .execute()
            segments = [t["video_url"] for t in sorted(completed, key=lambda x: int(x.get("clip_idx") or 0))]
            try:
                try:
                    from .video_stitcher import stitch_video_segments
                except ImportError:
                    from video_stitcher import stitch_video_segments
                final_url = await stitch_video_segments(segments, run_id, f"{run_id}_final.mp4")
                self.supabase.table("crew_sessions")\
                    .update({
                        "status": "completed",
                        "result": final_url,
                        "updated_at": datetime.utcnow().isoformat()
                    })\
                    .eq("run_id", run_id)\
                    .execute()
            except Exception as e:
                self.supabase.table("crew_sessions")\
                    .update({
                        "status": "failed",
                        "error": str(e),
                        "updated_at": datetime.utcnow().isoformat()
                    })\
                    .eq("run_id", run_id)\
                    .execute()


def get_session_manager():
    global _manager
    if _manager is not None:
        return _manager
    try:
        from supabase import create_client
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_ANON_KEY")
        supabase = create_client(url, key) if url and key else None
        _manager = SessionManager(supabase)
        return _manager
    except Exception:
        return None
