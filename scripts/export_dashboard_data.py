#!/usr/bin/env python3
"""Export the TubePilot AI Google Sheets store to a public JSON snapshot.

Reads the 8 yt_* tabs from the "TubePilot AI Data" spreadsheet (via the
repo's own ``db.sheets`` store, service-account auth from env) and writes
``dashboard-data.json`` — the data file the web dashboard fetches.

Runs in GitHub Actions (see ``.github/workflows/dashboard.yml``) with the
repo secrets ``GOOGLE_SHEETS_ID`` / ``GOOGLE_SERVICE_ACCOUNT_JSON``.

The Sheets store holds NO secrets by design (secrets live in repo secrets
and env only), so this JSON is safe to publish in the public repo.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import sheets  # noqa: E402

TABS = list(sheets.TAB_HEADERS.keys())
TTL_TABS = {"yt_research_cache", "yt_verification_log", "yt_pipeline_runs", "yt_bot_requests"}


def _is_expired_row(row: dict) -> bool:
    """TTL parity with the app: a row is expired when its ttl epoch passed.

    Cells arrive as strings from Sheets, so coerce before comparing.
    """
    ttl = row.get("ttl")
    if ttl is None or ttl == "":
        return False
    try:
        return float(ttl) <= datetime.now(timezone.utc).timestamp()
    except (TypeError, ValueError):
        return False


def _json_safe(value):
    """Coerce a Sheets-decoded cell value to something json.dumps accepts."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return str(value)


def export() -> dict:
    tabs = {}
    for tab in TABS:
        rows = sheets._read_rows(tab)
        if tab in TTL_TABS:
            rows = [r for r in rows if not _is_expired_row(r)]
        rows = [{k: _json_safe(v) for k, v in row.items()} for row in rows]
        tabs[tab] = {"count": len(rows), "rows": rows}

    def _by_status(rows, key="status"):
        out = {}
        for r in rows:
            out[str(r.get(key) or "unknown")] = out.get(str(r.get(key) or "unknown"), 0) + 1
        return out

    videos = tabs["yt_videos"]["rows"]
    queue = tabs["yt_topics_queue"]["rows"]
    requests = tabs["yt_bot_requests"]["rows"]
    runs = tabs["yt_pipeline_runs"]["rows"]
    config = {r.get("key"): r.get("value_json") for r in tabs["yt_config"]["rows"] if r.get("key")}

    now_utc = datetime.now(timezone.utc)
    return {
        "generated_at_utc": now_utc.isoformat(),
        "spreadsheet_id": sheets._get_spreadsheet_id(),
        "tabs": tabs,
        "summary": {
            "videos_total": len(videos),
            "videos_by_status": _by_status(videos),
            "queue_total": len(queue),
            "queue_by_status": _by_status(queue),
            "pending_requests": sum(1 for r in requests if (r.get("status") or "") == "pending"),
            "requests_total": len(requests),
            "pipeline_runs_total": len(runs),
            "config_keys": sorted(config.keys()),
            "active_models": config.get("active_models"),
        },
    }


def main() -> int:
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("dashboard-data.json")
    data = export()
    out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(
        f"Wrote {out_path} "
        f"(videos={data['summary']['videos_total']}, "
        f"queue={data['summary']['queue_total']}, "
        f"requests={data['summary']['requests_total']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
