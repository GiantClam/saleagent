import logging
from typing import List, Dict, Any
from .base import BaseGenerator

logger = logging.getLogger("workflow")

class MultiGenerator(BaseGenerator):
    """
    A generator that wraps multiple candidate generators and tries them in order.
    Provides automatic fallback logic.
    """
    def __init__(self, generators: List[BaseGenerator]):
        self.generators = generators
        self._active_generator_index = 0

    async def generate(self, **kwargs) -> Dict[str, Any]:
        last_error = "No generators available"
        
        for i, gen in enumerate(self.generators):
            logger.info(f"[MultiGenerator] Trying generator {i+1}/{len(self.generators)}")
            result = await gen.generate(**kwargs)
            
            if result.get("status") != "failed":
                # Success or pending, record which one worked for status checks
                result["_generator_index"] = i
                return result
            
            last_error = result.get("error")
            logger.warning(f"[MultiGenerator] Generator {i+1} failed: {last_error}. Falling back...")

        return {"status": "failed", "error": f"All generators failed. Last error: {last_error}"}

    async def get_status(self, task_metadata: Dict[str, Any]) -> Dict[str, Any]:
        """
        task_metadata should contain task_id and _generator_index.
        """
        idx = task_metadata.get("_generator_index", 0)
        task_id = task_metadata.get("task_id")
        
        if idx >= len(self.generators):
            return {"status": "failed", "error": "Invalid generator index"}
            
        return await self.generators[idx].get_status(task_id)
