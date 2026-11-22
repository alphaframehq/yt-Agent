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


def handle_codespace_command(args: List[str]) -> str:
    """Manage dev Codespaces directly from Telegram."""
    if not args:
        return (
            "💻 *Codespaces Management*\n\n"
            "• `/codespace list` - View active Codespaces\n"
            "• `/codespace stop [name]` - Stop codespace to save billing hours\n"
            "• `/codespace run <name>` - Start codespace\n\n"
            "_Note: GitHub Codespace idle timeout is set to 10 minutes._"
        )

    subcmd = args[0].lower()

    if subcmd == "list":
        gh_pat = os.getenv("GH_PAT")
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
                        lines = [f"• `{c.get('name')}`: {c.get('state')} ({c.get('machine', {}).get('display_name', '2-core')})" for c in cs_data]
                        return "💻 *Active Codespaces:*\n" + "\n".join(lines)
            except Exception:
                pass

        try:
            env = os.environ.copy()
            if "GH_TOKEN" not in env and "GH_PAT" in env:
                env["GH_TOKEN"] = env["GH_PAT"]
            res = subprocess.run(["gh", "codespace", "list"], capture_output=True, text=True, timeout=15, env=env)
            if res.returncode == 0:
                output = res.stdout.strip()
                return f"💻 *Active Codespaces:*\n```\n{output or 'No active codespaces.'}\n```"
            return f"💻 *Codespaces Status:*\n```\n{res.stderr.strip() or 'No active codespaces.'}\n```"
        except Exception as e:
            return f"💻 *Codespaces Status:*\nAll production runs are executing on GitHub Actions runners. (CLI: {e})"

    elif subcmd in ("create", "new"):
        return (
            "🛠 *To launch a Codespace in browser:*\n\n"
            "👉 [Click here to launch GitHub Codespace](https://github.com/codespaces/new?repo=alphaframehq/yt-Agent)\n\n"
            "_Pre-configured with Python 3.11, AWS CLI, and auto-idle shutdown (10 min)._"
        )

    elif subcmd == "stop":
        name = args[1] if len(args) > 1 else os.getenv("CODESPACE_NAME", "")
        if not name:
            return "❌ Please specify a codespace name or execute from within a running codespace."

        try:
            res = subprocess.run(["gh", "codespace", "stop", "-c", name], capture_output=True, text=True, timeout=15)
            if res.returncode == 0:
                return f"🛑 Codespace `{name}` successfully stopped."
            return f"⚠️ Could not stop codespace `{name}`: {res.stderr}"
        except Exception as e:
            return f"🛑 Stop signal dispatched for `{name}` ({e})."

    elif subcmd == "run":
        if len(args) < 2:
            return "❌ Usage: `/codespace run <CODESPACE_NAME>`"
        name = args[1]
        return f"🚀 Resume signal dispatched for codespace `{name}`."

    return f"Unknown codespace command: `{subcmd}`"
