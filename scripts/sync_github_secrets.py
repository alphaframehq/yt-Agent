"""Sync local .env secrets to GitHub Actions Repository Secrets."""

import base64
import os
import sys
import httpx
from nacl import encoding, public

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
env_path = os.path.join(_PROJECT_ROOT, ".env")

if not os.path.exists(env_path):
    print("No .env found at", env_path)
    sys.exit(1)

# Load env
from dotenv import dotenv_values
env_vars = dotenv_values(env_path)

pat = env_vars.get("GH_PAT")
owner = env_vars.get("GH_REPO_OWNER", "alphaframehq")
repo = env_vars.get("GH_REPO_NAME", "yt-Agent")

if not pat:
    print("GH_PAT missing from .env")
    sys.exit(1)

client = httpx.Client(
    headers={
        "Authorization": f"Bearer {pat}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "TubePilot-Sync",
    },
    timeout=20.0,
)

# 1. Get repo public key for secrets
res = client.get(f"https://api.github.com/repos/{owner}/{repo}/actions/secrets/public-key")
if res.status_code != 200:
    print(f"Failed to fetch public key ({res.status_code}): {res.text}")
    sys.exit(1)

key_data = res.json()
public_key_b64 = key_data["key"]
key_id = key_data["key_id"]
public_key = public.PublicKey(public_key_b64.encode("utf-8"), encoding.Base64Encoder())

# 2. Encrypt and upload each secret
SECRETS_TO_SYNC = [
    "GOOGLE_SHEETS_ID",
    "GOOGLE_SERVICE_ACCOUNT_JSON",
    "GH_PAT",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ADMIN_ID",
    "AWS_REGION",
    "GEMINI_KEY",
    "OPENROUTER_KEY",
    "PIXABAY_KEY",
    "TINYFISH_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "YT_CLIENT_ID",
    "YT_CLIENT_SECRET",
    "YT_REFRESH_TOKEN",
    "FAL_KEY",
    "REPLICATE_API_TOKEN",
]

sealed_box = public.SealedBox(public_key)

print(f"=== Syncing Secrets to GitHub Repo: {owner}/{repo} ===")
for sec_name in SECRETS_TO_SYNC:
    val = env_vars.get(sec_name, "").strip()
    if not val:
        continue

    # Encrypt
    encrypted = sealed_box.encrypt(val.encode("utf-8"))
    encrypted_b64 = base64.b64encode(encrypted).decode("utf-8")

    # Put secret
    put_res = client.put(
        f"https://api.github.com/repos/{owner}/{repo}/actions/secrets/{sec_name}",
        json={
            "encrypted_value": encrypted_b64,
            "key_id": key_id,
        },
    )
    if put_res.status_code in (201, 204):
        print(f"  [OK] Synced {sec_name}")
    else:
        print(f"  [FAILED] {sec_name} ({put_res.status_code}): {put_res.text}")

print("GitHub Actions secrets synchronization complete!")
