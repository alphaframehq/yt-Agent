"""In-memory fake Google Sheets service for CI tests.

Tests must never touch the real Sheets API (no credentials, no
network in CI). This module implements the small subset of the
``googleapiclient`` chained interface that ``db/sheets.py`` and
``db/setup_sheets.py`` use:

- ``service.spreadsheets().values().get/update/append/clear``
- ``service.spreadsheets().get`` (spreadsheet metadata / tab titles)
- ``service.spreadsheets().batchUpdate`` (addSheet)

Use :func:`install_fake_sheets` to patch ``db.sheets._get_service``
for a test module (start in ``setUpModule``, stop in
``tearDownModule``), or use :class:`FakeSheetsService` directly with
``unittest.mock.patch``.

The fake stores tabs as ``{title: [[cell, ...], ...]}`` (first row is
the header). ``fake.store`` is exposed so tests can assert on the raw
rows that were written.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List
from unittest.mock import patch

_RANGE_RE = re.compile(r"^([^!]+)!([A-Z]+)(\d+)?(?::([A-Z]+)(\d+)?)?$")

def _parse_range(range_: str) -> tuple:
    """Parse 'Tab!A1:Z10' -> (tab, start_row, end_row|None)."""
    m = _RANGE_RE.match(range_)
    if not m:
        raise ValueError(f"FakeSheetsService cannot parse range {range_!r}")
    tab, _col_a, row_a, _col_b, row_b = m.groups()
    start = int(row_a) if row_a else 1
    end = int(row_b) if row_b else None
    return tab, start, end


class _FakeRequest:
    def __init__(self, payload: Any):
        self._payload = payload

    def execute(self) -> Any:
        return self._payload


class _FakeValues:
    def __init__(self, store: Dict[str, List[List[Any]]]):
        self._store = store

    def get(self, spreadsheetId=None, range=None, **kwargs):
        tab, start, end = _parse_range(range)
        rows = self._store.get(tab, [])
        selected = rows[start - 1 : end] if end is not None else rows[start - 1 :]
        return _FakeRequest({"values": [list(r) for r in selected]})

    def update(self, spreadsheetId=None, range=None, valueInputOption=None, body=None, **kwargs):
        tab, start, _end = _parse_range(range)
        rows = self._store.setdefault(tab, [])
        values = body.get("values", [])
        for i, row in enumerate(values):
            idx = start - 1 + i
            while len(rows) <= idx:
                rows.append([])
            rows[idx] = list(row)
        return _FakeRequest({"updatedRange": range})

    def append(self, spreadsheetId=None, range=None, valueInputOption=None, insertDataOption=None, body=None, **kwargs):
        tab, _start, _end = _parse_range(range)
        rows = self._store.setdefault(tab, [])
        for row in body.get("values", []):
            rows.append(list(row))
        return _FakeRequest({"updates": {"updatedRows": len(body.get("values", []))}})

    def clear(self, spreadsheetId=None, range=None, **kwargs):
        tab, start, end = _parse_range(range)
        rows = self._store.get(tab, [])
        if end is None:
            del rows[start - 1 :]
        else:
            del rows[start - 1 : end]
        return _FakeRequest({})


class _FakeSpreadsheets:
    def __init__(self, store: Dict[str, List[List[Any]]]):
        self._store = store

    def values(self) -> _FakeValues:
        return _FakeValues(self._store)

    def get(self, spreadsheetId=None, **kwargs):
        sheets = [{"properties": {"title": t}} for t in self._store.keys()]
        return _FakeRequest({"sheets": sheets})

    def batchUpdate(self, spreadsheetId=None, body=None, **kwargs):
        for req in (body or {}).get("requests", []):
            add = req.get("addSheet")
            if add:
                title = add["properties"]["title"]
                self._store.setdefault(title, [])
        return _FakeRequest({})


class FakeSheetsService:
    """In-memory stand-in for the googleapiclient Sheets service."""

    def __init__(self):
        self.store: Dict[str, List[List[Any]]] = {}

    def spreadsheets(self) -> _FakeSpreadsheets:
        return _FakeSpreadsheets(self.store)


def install_fake_sheets() -> tuple:
    """Patch ``db.sheets._get_service`` with a fake for the test module.

    Also ensures ``GOOGLE_SHEETS_ID`` is set to a dummy value (the
    production code validates its presence before calling the
    service). Returns ``(fake, patcher)`` — call ``patcher.stop()``
    in ``tearDownModule``.
    """
    os.environ.setdefault("GOOGLE_SHEETS_ID", "test-spreadsheet-id")
    os.environ.setdefault("GOOGLE_SERVICE_ACCOUNT_JSON", '{"type": "service_account"}')
    fake = FakeSheetsService()
    patcher = patch("db.sheets._get_service", return_value=fake)
    patcher.start()
    return fake, patcher
