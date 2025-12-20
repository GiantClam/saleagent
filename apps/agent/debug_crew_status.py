import requests
import json

def debug_status():
    run_id = "3503d017-a0aa-4cff-8167-706ee76f5379"
    url = f"http://localhost:8000/workflow/crew-status/{run_id}"
    
    print(f"Calling {url}...")
    try:
        resp = requests.get(url)
        print(f"Status Code: {resp.status_code}")
        try:
            print(f"Response: {json.dumps(resp.json(), indent=2, ensure_ascii=False)}")
        except:
            print(f"Raw Response: {resp.text}")
    except Exception as e:
        print(f"Request failed: {e}")

if __name__ == "__main__":
    debug_status()
