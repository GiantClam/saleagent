
import os
import asyncio
import json
from dotenv import load_dotenv

# Load env from .env if present
load_dotenv()

async def main():
    try:
        from supabase import create_client
    except ImportError:
        print("Please install supabase: pip install supabase")
        return

    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_ANON_KEY")
    
    print(f"URL: {url}")
    print(f"URL: {url}")
    
    key_source = "None"
    if os.getenv("SUPABASE_SERVICE_ROLE_KEY"):
        key_source = "SUPABASE_SERVICE_ROLE_KEY"
    elif os.getenv("SUPABASE_SERVICE_KEY"):
        key_source = "SUPABASE_SERVICE_KEY"
    elif os.getenv("SUPABASE_ANON_KEY"):
        key_source = "SUPABASE_ANON_KEY"
        
    print(f"Key used from: {key_source}")
    print(f"Key prefix: {key[:10]}...")
    
    # Decode JWT to check role
    try:
        import base64
        # JWT is header.payload.signature
        parts = key.split(".")
        if len(parts) > 1:
            payload = parts[1]
            # Pad base64
            payload += "=" * ((4 - len(payload) % 4) % 4)
            data = json.loads(base64.urlsafe_b64decode(payload).decode())
            print(f"JWT Role: {data.get('role', 'unknown')}")
            print(f"JWT Iss: {data.get('iss', 'unknown')}")
    except Exception as e:
        print(f"Failed to decode JWT: {e}")

    if not url or not key:
        print("Missing SUPABASE_URL or Key")
        return

    supabase = create_client(url, key)
    
    run_id = "838cdce1-bca8-40f0-abf4-9e92a1dcbdba"
    
    with open("d:\\github\\saleagent\\debug_output.txt", "w", encoding="utf-8") as f:
        f.write(f"URL: {url}\n")
        f.write(f"Key source: {key_source}\n")
        f.write(f"Querying for run_id: {run_id}\n")
        
        # 1. Try exact match
        res = supabase.table("crew_sessions").select("*").eq("run_id", run_id).execute()
        f.write(f"Exact match count: {len(res.data)}\n")
        if res.data:
            f.write(f"Data: {json.dumps(res.data[0], default=str)}\n")
        else:
            f.write("No data found for exact match.\n")
            
        # 2. Try listing recent sessions
        res2 = supabase.table("crew_sessions").select("run_id, created_at, status").order("created_at", desc=True).limit(5).execute()
        f.write("\nRecent sessions:\n")
        for row in res2.data:
            f.write(f"Time: {row.get('created_at')}, ID: {row.get('run_id')}, Status: {row.get('status')}\n")
            
    print("Debug output written to debug_output.txt")

if __name__ == "__main__":
    asyncio.run(main())
