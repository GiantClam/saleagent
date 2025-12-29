from typing import Protocol, Dict, Any, Optional, List

class BaseGenerator(Protocol):
    """
    Standard interface for all AI generation providers (Image, Video, Avatar).
    """
    async def generate(self, **kwargs) -> Dict[str, Any]:
        """
        Submits a generation task and returns the result (or pending status).
        
        Return format:
        {
            "status": "success" | "pending" | "failed",
            "task_id": str,
            "url": str (if success),
            "error": str (if failed)
        }
        """
        ...

    async def get_status(self, task_id: str) -> Dict[str, Any]:
        """
        Checks the status of an asynchronous task.
        """
        ...
