
import asyncio
import httpx
import json
import sys

BASE_URL = "http://localhost:8000"

async def run_verification():
    run_id = "verify_test_" + str(int(asyncio.get_event_loop().time()))
    print(f"Starting verification for run_id: {run_id}")

    async with httpx.AsyncClient(timeout=30.0) as client:
        # 1. Plan
        print("\n--- Testing /workflow/plan ---")
        plan_payload = {
            "run_id": run_id,
            "collected_info": {
                "theme": "A futuristic city with flying cars",
                "styles": ["Cyberpunk", "Cinematic"],
                "duration": 5.0
            }
        }
        
        storyboard = None
        try:
            resp = await client.post(f"{BASE_URL}/workflow/plan", json=plan_payload)
            if resp.status_code != 200:
                print(f"FAIL: Plan Request Failed with status: {resp.status_code}")
                try:
                    print(f"Response: {resp.text}")
                except:
                    print(f"Response (bytes): {resp.content}")
                return
            
            plan_data = resp.json()
            print("Plan Response:", json.dumps(plan_data, indent=2))
            storyboard = plan_data.get("storyboard")
            if not storyboard:
                print("FAIL: Verification Failed: No storyboard in plan response")
                return
        except Exception as e:
            print(f"FAIL: Plan Request Exception: {e}")
            return

        # 2. Confirm
        print("\n--- Testing /workflow/confirm ---")
        confirm_payload = {
            "run_id": run_id,
            "storyboard": storyboard
        }
        try:
            resp = await client.post(f"{BASE_URL}/workflow/confirm", json=confirm_payload)
            if resp.status_code != 200:
                print(f"FAIL: Confirm Request Failed with status: {resp.status_code}")
                try:
                    print(f"Response: {resp.text}")
                except:
                    print(f"Response (bytes): {resp.content}")
                return
            
            confirm_data = resp.json()
            print("Confirm Response:", json.dumps(confirm_data, indent=2))
        except Exception as e:
            print(f"FAIL: Confirm Request Exception: {e}")
            return

        # 3. Run Clips (SSE)
        print("\n--- Testing /workflow/run-clips (SSE) ---")
        run_clips_payload = {"run_id": run_id, "storyboards": storyboard.get("scenes", [])}
        
        try:
            async with client.stream("POST", f"{BASE_URL}/workflow/run-clips", json=run_clips_payload, timeout=60.0, headers={"Accept": "text/event-stream"}) as response:
                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        data_str = line[len("data:"):].strip()
                        print(f"SSE Data: {data_str}")
                        try:
                            event = json.loads(data_str)
                            if event.get("type") == "error":
                                print(f"FAIL: Received Error Event: {event}")
                                break
                            if event.get("type") == "run_finished":
                                print("SUCCESS: Workflow Finished Successfully")
                                break
                            if event.get("type") == "done":
                                print("SUCCESS: Workflow Done (Legacy Event)")
                                break
                        except:
                            pass
        except Exception as e:
             print(f"FAIL: SSE Stream Failed: {e}")

if __name__ == "__main__":
    asyncio.run(run_verification())
