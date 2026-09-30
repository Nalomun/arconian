"""
Re-authenticate with Schwab and save a fresh token to schwab_token.json.

Run this whenever the refresh token expires (every 7 days):

    cd arconian
    python scripts/reauth.py

A browser window will open to the Schwab login page. After you log in and
authorise the app, schwab-py catches the redirect and writes a new token file.
"""

import sys
from pathlib import Path

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

import os
import schwab

API_KEY      = os.environ["SCHWAB_API_KEY"].strip()
APP_SECRET   = os.environ["SCHWAB_APP_SECRET"].strip()
TOKEN_PATH   = os.environ.get("SCHWAB_TOKEN_PATH", "schwab_token.json").strip()
CALLBACK_URL = "https://127.0.0.1:8182/"

if __name__ == "__main__":
    print(f"Token will be saved to: {TOKEN_PATH}")
    print()

    client = schwab.auth.client_from_manual_flow(
        api_key=API_KEY,
        app_secret=APP_SECRET,
        callback_url=CALLBACK_URL,
        token_path=TOKEN_PATH,
    )

    print()
    print("Authentication successful — token saved.")
    print("You can now run: python main.py --dry-run")
