#!/usr/bin/env python3
"""Step 1: Generate the Google OAuth URL for Docs write access."""
import json, os, pickle
from google_auth_oauthlib.flow import InstalledAppFlow

CREDS_DIR = "/home/ubuntu/.config/mcp-gdrive"
KEYS_PATH = os.path.join(CREDS_DIR, "gcp-oauth.keys.json")
FLOW_PATH = os.path.join(CREDS_DIR, ".auth_flow.pickle")
URL_PATH = os.path.join(CREDS_DIR, ".auth_url.txt")
SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/documents",
]

flow = InstalledAppFlow.from_client_secrets_file(KEYS_PATH, SCOPES)
auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")

# Save flow state
with open(FLOW_PATH, "wb") as f:
    pickle.dump(flow, f)

# Save URL for reference
with open(URL_PATH, "w") as f:
    f.write(auth_url)

print("=" * 60)
print("STEP 1: Visit this URL to authorize Google Docs write access:")
print(auth_url)
print("=" * 60)
print(f"\nFlow saved. After authorization, run step 2 with the code.")
print(f"python3 /home/ubuntu/work/next-step-bot/scripts/gdocs_auth_step2.py CODE")
