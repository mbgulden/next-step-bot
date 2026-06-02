#!/usr/bin/env python3
"""Step 2: Exchange the OAuth code for credentials."""
import os, pickle, sys
from google.auth.transport.requests import Request

CREDS_DIR = "/home/ubuntu/.config/mcp-gdrive"
FLOW_PATH = os.path.join(CREDS_DIR, ".auth_flow.pickle")
TOKEN_PATH = os.path.join(CREDS_DIR, ".gdocs-write-token.pickle")

if len(sys.argv) < 2:
    print("Usage: python3 gdocs_auth_step2.py YOUR_AUTH_CODE")
    sys.exit(1)

code = sys.argv[1].strip()

with open(FLOW_PATH, "rb") as f:
    flow = pickle.load(f)

flow.fetch_token(code=code)
creds = flow.credentials

with open(TOKEN_PATH, "wb") as f:
    pickle.dump(creds, f)

# Clean up
os.remove(FLOW_PATH)

print("✅ Google Docs write access authorized!")
print(f"   Token saved to {TOKEN_PATH}")
print(f"   Scopes: {creds.scopes}")
