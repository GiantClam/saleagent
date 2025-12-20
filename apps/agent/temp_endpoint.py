
@app.get("/workflow/crew-status/{run_id}")
async def workflow_crew_status(run_id: str):
    if not supabase:
        return {"error": "Supabase not configured"}
    try:
        # 尝试查询任务状态
        res = supabase.table("jobs").select("run_id, status, result, error, created_at, updated_at").eq("run_id", run_id).single().execute()
        return res.data or {}
    except Exception as e:
        # 如果找不到任务，返回一个默认状态而不是报错，避免前端轮询炸裂
        # PGRST116 means 0 rows
        logger.warning(f"[crew-status] Job {run_id} not found: {e}")
        return {
            "run_id": run_id,
            "status": "pending", 
            "result": None,
            "error": "Job not initialization yet"
        }
