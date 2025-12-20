
import os
import sys
from dotenv import load_dotenv

# Ensure we can import from the current directory
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# Load environment variables
load_dotenv()

from r2 import get_r2_client

def configure_cors():
    r2 = get_r2_client()
    bucket = os.getenv("R2_BUCKET", "video")
    
    if not r2:
        print("Error: R2 client not configured. Check your env vars.")
        return

    print(f"Configuring CORS for bucket: {bucket}")
    
    cors_configuration = {
        'CORSRules': [{
            'AllowedHeaders': ['*'],
            'AllowedMethods': ['PUT', 'POST', 'GET', 'HEAD', 'DELETE'],
            'AllowedOrigins': [
                'http://localhost:3000', 
                'http://127.0.0.1:3000',
                'http://localhost:8000',
                'http://localhost',
                '*'
            ],
            'ExposeHeaders': ['ETag'],
            'MaxAgeSeconds': 3000
        }]
    }

    try:
        r2.put_bucket_cors(Bucket=bucket, CORSConfiguration=cors_configuration)
        print("Successfully applied CORS configuration.")
    except Exception as e:
        print(f"Failed to set CORS: {e}")

if __name__ == "__main__":
    configure_cors()
