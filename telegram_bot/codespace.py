"""GitHub Codespaces auto-stop and management command handler for TubePilot AI.

Production runs happen exclusively on GitHub Actions.
Codespaces are used strictly for manual development with strict idle timeouts.
Commands:
- /codespace list -> View active Codespaces
- /codespace stop [name] -> Stop current Codespace or target
- /codespace run <name> -> Start target Codespace
"""

from __future__ import annotations

import os
import subprocess
from typing import List
from loguru import logger

__all__ = ["handle_codespace_command"]


def _get_gh_pat() -> str:
    try:
        from pathlib import Path
        from dotenv import load_dotenv
        env_path = Path(__file__).resolve().parent.parent / ".env"
        if env_path.exists():
            load_dotenv(env_path)
    except Exception:
        pass

    try:
        from tools.secrets import get_secret
        val = get_secret("GH_PAT")
        if val:
            return val
    except Exception:
        pass
    return os.getenv("GH_PAT", "")


def handle_codespace_command(args: List[str]) -> str:
    """Manage dev Codespaces directly from Telegram."""
    if not args:
        return (
            "💻 *Codespaces Management*\n\n"
            "• `/codespace list` - View active Codespaces\n"
            "• `/codespace create` - Provision a new cloud dev environment\n"
            "• `/codespace stop [name]` - Stop codespace to save billing hours\n"
            "• `/codespace run [name]` - Start target codespace\n\n"
            "_Note: GitHub Codespace idle timeout is set to 10 minutes._"
        )

    subcmd = args[0].lower()
    gh_pat = _get_gh_pat()

    if subcmd == "list":
        if gh_pat:
            try:
                import httpx
                with httpx.Client(timeout=10.0) as client:
                    resp = client.get(
                        "https://api.github.com/user/codespaces",
                        headers={"Authorization": f"Bearer {gh_pat}", "Accept": "application/vnd.github.v3+json"},
                    )
                    if resp.status_code == 200:
                        cs_data = resp.json().get("codespaces", [])
                        if not cs_data:
                            return "💻 *Active Codespaces:*\n```\nNo active codespaces running. (0 billed hours)\n```"
                        lines = []
                        for c in cs_data:
                            name = c.get("name")
                            state = c.get("state")
                            web_url = c.get("web_url", "")
                            machine = c.get("machine", {}).get("display_name", "2 cores")
                            lines.append(f"• `{name}`: *{state}* ({machine})\n  🔗 [Open in Browser]({web_url})")
                        return "💻 *Active Codespaces:*\n\n" + "\n\n".join(lines)
            except Exception as e:
                logger.warning(f"Failed to query codespaces API: {e}")

        try:
            env = os.environ.copy()
            if "GH_TOKEN" not in env and gh_pat:
                env["GH_TOKEN"] = gh_pat
            res = subprocess.run(["gh", "codespace", "list"], capture_output=True, text=True, timeout=15, env=env)
            if res.returncode == 0:
                output = res.stdout.strip()
                return f"💻 *Active Codespaces:*\n```\n{output or 'No active codespaces.'}\n```"
            return f"💻 *Codespaces Status:*\n```\n{res.stderr.strip() or 'No active codespaces.'}\n```"
        except Exception as e:
            return f"💻 *Codespaces Status:*\nAll production runs are executing on GitHub Actions runners. (CLI: {e})"

    elif subcmd in ("create", "new"):
        owner = os.getenv("GH_REPO_OWNER", "alphaframehq")
        repo = os.getenv("GH_REPO_NAME", "yt-Agent")
        if gh_pat:
            try:
                import httpx
                with httpx.Client(timeout=15.0) as client:
                    res = client.post(
                        f"https://api.github.com/repos/{owner}/{repo}/codespaces",
                        headers={"Authorization": f"Bearer {gh_pat}", "Accept": "application/vnd.github.v3+json"},
                        json={"ref": "main", "machine": "basicLinux32gb", "idle_timeout_minutes": 10},
                    )
                    if res.status_code in (200, 201):
                        data = res.json()
                        cs_name = data.get("name")
                        web_url = data.get("web_url")
                        return (
                            f"✅ *Codespace Created Successfully!*\n\n"
                            f"• Name: `{cs_name}`\n"
                            f"• Status: *{data.get('state')}*\n"
                            f"• Specs: 2 Cores, 8 GB RAM, 32 GB SSD\n"
                            f"• Auto-idle timeout: 10 mins (saves billable hours)\n\n"
                            f"👉 [Click here to Open Codespace in Browser]({web_url})"
                        )
            except Exception as e:
                logger.warning(f"Direct API codespace creation failed: {e}")

        return (
            "🛠 *To launch a Codespace in browser:*\n\n"
            f"👉 [Click here to launch GitHub Codespace](https://github.com/codespaces/new?repo={owner}/{repo})\n\n"
            "_Pre-configured with Python 3.11, AWS CLI, and auto-idle shutdown (10 min)._"
        )

    elif subcmd == "stop":
        name = args[1] if len(args) > 1 else os.getenv("CODESPACE_NAME", "")
        if not name:
            return "❌ Please specify a codespace name or execute from within a running codespace: `/codespace stop <NAME>`"

        if gh_pat:
            try:
                import httpx
                with httpx.Client(timeout=10.0) as client:
                    resp = client.post(
                        f"https://api.github.com/user/codespaces/{name}/stop",
                        headers={"Authorization": f"Bearer {gh_pat}", "Accept": "application/vnd.github.v3+json"},
                    )
                    if resp.status_code in (200, 204):
                        return f"🛑 Codespace `{name}` successfully stopped. (Billing paused)"
            except Exception as e:
                logger.warning(f"REST API stop codespace notice: {e}")

        try:
            res = subprocess.run(["gh", "codespace", "stop", "-c", name], capture_output=True, text=True, timeout=15)
            if res.returncode == 0:
                return f"🛑 Codespace `{name}` successfully stopped."
            return f"⚠️ Could not stop codespace `{name}`: {res.stderr}"
        except Exception as e:
            return f"🛑 Stop signal dispatched for `{name}` ({e})."

    elif subcmd in ("run", "start"):
        name = args[1] if len(args) > 1 else ""
        if not name:
            return "❌ Usage: `/codespace run <CODESPACE_NAME>`"

        if gh_pat:
            try:
                import httpx
                with httpx.Client(timeout=10.0) as client:
                    resp = client.post(
                        f"https://api.github.com/user/codespaces/{name}/start",
                        headers={"Authorization": f"Bearer {gh_pat}", "Accept": "application/vnd.github.v3+json"},
                    )
                    if resp.status_code in (200, 204):
                        data = resp.json()
                        web_url = data.get("web_url")
                        return (
                            f"🚀 Codespace `{name}` starting up!\n\n"
                            f"👉 [Open in Browser]({web_url})"
                        )
            except Exception as e:
                logger.warning(f"REST API start codespace notice: {e}")

        return f"🚀 Resume signal dispatched for codespace `{name}`."

    return f"Unknown codespace command: `{subcmd}`"
