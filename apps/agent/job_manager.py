
import asyncio
import logging
import json
from datetime import datetime
from typing import Dict, Any, AsyncGenerator

logger = logging.getLogger("job_manager")

class JobManager:
    _instance = None
    
    def __init__(self):
        # run_id -> asyncio.Task
        self.active_tasks: Dict[str, asyncio.Task] = {}
        # run_id -> List[asyncio.Queue]
        self.event_subscribers: Dict[str, list[asyncio.Queue]] = {}
        # run_id -> latest_state (for check on connect)
        self.job_states: Dict[str, Dict[str, Any]] = {}

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = JobManager()
        return cls._instance

    async def start_job(self, run_id: str, task_coro):
        """Start a background task for a given run_id"""
        if run_id in self.active_tasks and not self.active_tasks[run_id].done():
            logger.warning(f"Job {run_id} is already running.")
            return
        
        # Create broadcast channels if not exist
        if run_id not in self.event_subscribers:
            self.event_subscribers[run_id] = []
            
        # Wrap task to handle cleanup
        async def wrapped_task():
            try:
                await self.broadcast(run_id, "System", "info", f"Job {run_id} started.")
                await task_coro
                await self.broadcast(run_id, "System", "done", "Job completed.")
            except Exception as e:
                logger.error(f"Job {run_id} failed: {e}", exc_info=True)
                await self.broadcast(run_id, "System", "error", f"Job failed: {str(e)}")
            finally:
                # Cleanup logic could go here, but we might want to keep history for a bit
                pass

        task = asyncio.create_task(wrapped_task())
        self.active_tasks[run_id] = task
        logger.info(f"Started job {run_id}")

    async def broadcast(self, run_id: str, agent: str, type: str, delta: str, payload: dict = None, progress: dict = None):
        """Broadcast an event to all subscribers of run_id"""
        event = {
            "run_id": run_id,
            "agent": agent,
            "type": type,
            "delta": delta,
            "payload": payload,
            "progress": progress,
            "timestamp": datetime.utcnow().isoformat()
        }
        
        # Update internal state (optional, simplified)
        if run_id not in self.job_states:
            self.job_states[run_id] = {}
        self.job_states[run_id]["last_event"] = event
        
        if run_id in self.event_subscribers:
            queues = self.event_subscribers[run_id]
            # Remove closed queues? complicated. iterate and put.
            for q in queues:
                await q.put(event)

    async def subscribe(self, run_id: str) -> AsyncGenerator[str, None]:
        """Yield SSE formatted events for a run_id"""
        q = asyncio.Queue()
        if run_id not in self.event_subscribers:
            self.event_subscribers[run_id] = []
        self.event_subscribers[run_id].append(q)
        
        try:
            # Yield initial ping or state?
            yield f"event: ping\ndata: {datetime.utcnow().isoformat()}\n\n"
            
            while True:
                event = await q.get()
                # Simple SSE format
                yield f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                
                # Exit condition? 
                if event['type'] in ['done', 'error', 'run_finished']:
                    # Give client a moment to receive it? 
                    # Usually we keep stream open or client closes.
                    pass
        except asyncio.CancelledError:
            # Client disconnected
            pass
        finally:
            if run_id in self.event_subscribers:
                self.event_subscribers[run_id].remove(q)

job_manager = JobManager.get_instance()
