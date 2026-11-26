"""Google Sheets 10-Tab Provisioned Setup Script for TubePilot AI.

Replaces ``db/setup_dynamo.py`` (25 Nov 2026 — DynamoDB removed
entirely). Creates the 10 ``yt_*`` tabs in the spreadsheet named by
``GOOGLE_SHEETS_ID`` (see ``db/sheets.py`` ``TAB_HEADERS``) and ensures
each tab's first row holds the expected headers.

Tabs:
1. yt_videos
2. yt_research_cache
3. yt_verification_log
4. yt_pipeline_runs
5. yt_config (config rows only — no secrets; secrets live in GitHub
   repo secrets / env vars)
6. yt_bot_requests
7. yt_topics_queue
8. yt_insights
9. yt_trends (daily trend-research reports)
10. yt_shorts (Shorts upload/status tracking, separate from yt_videos)

Auth: service-account JSON from ``GOOGLE_SERVICE_ACCOUNT_JSON`` (raw
JSON string or path to a ``.json`` key file). The service account must
be shared on the spreadsheet as Editor.

Missing env vars, a missing spreadsheet, a failed tab create, or a
tab whose headers do not match the expected schema all raise — this
script never returns a "skipped"/"failed" dict.
"""

from __future__ import annotations

from typing import Dict, List
from loguru import logger

import db.sheets as _sheets
from db.sheets import TAB_HEADERS

_TAB_NAMES: List[str] = list(TAB_HEADERS.keys())


def _existing_titles(service: object, sheet_id: str) -> List[str]:
    """Return the titles of the tabs already in the spreadsheet."""
    try:
        meta = (
            service.spreadsheets()  # type: ignore[attr-defined]
            .get(spreadsheetId=sheet_id)
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"Could not read spreadsheet '{sheet_id}': {exc}. Check "
            "GOOGLE_SHEETS_ID and that the service account is shared "
            "on the spreadsheet as Editor."
        ) from exc
    return [s["properties"]["title"] for s in meta.get("sheets", [])]


def _create_tab(service: object, sheet_id: str, title: str) -> None:
    """Create one tab (idempotent — skips if it already exists)."""
    try:
        service.spreadsheets().batchUpdate(  # type: ignore[attr-defined]
            spreadsheetId=sheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": title}}}]},
        ).execute()
        logger.info("Google Sheets tab '{}' created.", title)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Failed to create Google Sheets tab '{title}': {exc}") from exc


def _ensure_headers(service: object, sheet_id: str, title: str) -> None:
    """Ensure the tab's first row holds the expected headers.

    Raises if the tab already has a different header row — a silently
    rewritten header would corrupt existing data.
    """
    expected = TAB_HEADERS[title]
    try:
        resp = (
            service.spreadsheets()  # type: ignore[attr-defined]
            .values()
            .get(spreadsheetId=sheet_id, range=f"{title}!A1:Z1")
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Could not read headers of tab '{title}': {exc}") from exc
    rows = resp.get("values", [])
    if not rows or not any(c != "" for c in rows[0]):
        service.spreadsheets().values().update(  # type: ignore[attr-defined]
            spreadsheetId=sheet_id,
            range=f"{title}!A1",
            valueInputOption="RAW",
            body={"values": [expected]},
        ).execute()
        logger.info("Google Sheets tab '{}' headers written.", title)
        return
    if list(rows[0]) != expected:
        raise RuntimeError(
            f"Google Sheets tab '{title}' has unexpected headers "
            f"{rows[0]} (expected {expected}). Fix the tab by hand or "
            "point GOOGLE_SHEETS_ID at a fresh spreadsheet."
        )


def setup_all_sheets() -> Dict[str, str]:
    """Idempotently create all tabs with headers and verify them."""
    # Resolved via the db.sheets module attribute (not a from-import)
    # so tests can patch db.sheets._get_service with a fake.
    service = _sheets._get_service()  # raises if env/credentials are missing
    sheet_id = _sheets._get_spreadsheet_id()

    existing = _existing_titles(service, sheet_id)
    for title in _TAB_NAMES:
        if title not in existing:
            _create_tab(service, sheet_id, title)
        _ensure_headers(service, sheet_id, title)

    # Verify every tab actually exists with the right headers.
    titles = _existing_titles(service, sheet_id)
    missing = [t for t in _TAB_NAMES if t not in titles]
    if missing:
        raise RuntimeError(
            f"Google Sheets tabs missing after setup: {', '.join(missing)}"
        )

    logger.info("All %d Google Sheets tab configurations verified.", len(_TAB_NAMES))
    return {"status": "success", "tabs": f"{len(_TAB_NAMES)} tabs configured with headers"}


if __name__ == "__main__":
    setup_all_sheets()
