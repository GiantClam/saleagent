import os
import asyncio
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()
load_dotenv('.env')

async def check_duplicates():
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_SERVICE_KEY")
    if not url or not key:
        print("Missing Supabase config")
        return

    supabase = create_client(url, key)
    run_id = "586a1e12-cbaa-40f2-9658-c05f1086facd"
    
    print(f"Checking tasks for run_id: {run_id}")
    
    res = supabase.table("video_tasks").select("*").eq("run_id", run_id).execute()
    tasks = res.data or []
    
    print(f"Found {len(tasks)} tasks:")
    for t in tasks:
        print(f"ID: {t.get('id')}, Clip: {t.get('clip_idx')}, Status: {t.get('status')}, Updated: {t.get('updated_at')}")

if __name__ == "__main__":
    asyncio.run(check_duplicates())
