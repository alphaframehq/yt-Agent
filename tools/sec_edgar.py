"""SEC EDGAR integration tool for TubePilot AI.

Extracts verifiable 10-K and 10-Q filing data for public companies.
Adheres strictly to SEC EDGAR API fair-access rules and User-Agent requirements.
Provides traceable claim IDs and URLs for anti-hallucination compliance.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional
import httpx
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

__all__ = [
    "get_company_facts",
    "search_company_filings",
]

CACHE_DIR = Path("cache/sec_edgar")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# SEC requires specific User-Agent format: Sample Company Name AdminContact@<sample company domain>.com
SEC_USER_AGENT = "TubePilotAI research@alphapilot.io"
HEADERS = {
    "User-Agent": SEC_USER_AGENT,
    "Accept-Encoding": "gzip, deflate",
}


def _get_cache_path(key: str) -> Path:
    h = hashlib.md5(key.encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{h}.json"


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=6))
def get_company_facts(cik: str) -> Dict[str, Any]:
    """Retrieve full company facts (financial statements) by CIK number from SEC EDGAR.
    
    CIK must be 10 digits zero-padded (e.g. '0001018724' for Amazon).
    """
    padded_cik = cik.zfill(10)
    cache_file = _get_cache_path(f"facts:{padded_cik}")

    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{padded_cik}.json"
    logger.info(f"Querying SEC EDGAR company facts for CIK: {padded_cik}")

    with httpx.Client(headers=HEADERS, timeout=30.0) as client:
        resp = client.get(url)
        if resp.status_code == 404:
            logger.warning(f"No SEC facts found for CIK {padded_cik}")
            return {}
        resp.raise_for_status()
        data = resp.json()
        cache_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return data


def extract_annual_revenues(facts_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract verified annual revenues and accession numbers from SEC facts data."""
    revenues = []
    us_gaap = facts_data.get("facts", {}).get("us-gaap", {})
    
    # Common revenue tags in US-GAAP taxonomy
    rev_nodes = (
        us_gaap.get("Revenues", {})
        or us_gaap.get("RevenueFromContractWithCustomerExcludingAssessedTax", {})
        or us_gaap.get("SalesRevenueNet", {})
    )

    units = rev_nodes.get("units", {}).get("USD", [])
    for entry in units:
        if entry.get("form") == "10-K" and "frame" in entry:
            frame = entry.get("frame")
            revenues.append({
                "year": frame.replace("CY", ""),
                "val": float(entry.get("val", 0.0)),
                "accNum": entry.get("accNum"),
                "filed": entry.get("filed"),
                "source_url": f"https://www.sec.gov/edgar/browse/?CIK={facts_data.get('cik')}",
            })
    return revenues


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=6))
def search_company_filings(ticker: str) -> Dict[str, Any]:
    """Resolve ticker symbol to CIK and fetch recent filing metadata."""
    cache_file = _get_cache_path(f"ticker:{ticker.upper()}")
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    # SEC tickers directory
    url = "https://www.sec.gov/files/company_tickers.json"
    with httpx.Client(headers=HEADERS, timeout=30.0) as client:
        resp = client.get(url)
        resp.raise_for_status()
        tickers_data = resp.json()

        for idx, item in tickers_data.items():
            if item.get("ticker", "").upper() == ticker.upper():
                cik = str(item.get("cik_str")).zfill(10)
                result = {
                    "ticker": ticker.upper(),
                    "cik": cik,
                    "title": item.get("title"),
                    "sec_url": f"https://www.sec.gov/edgar/browse/?CIK={cik}",
                }
                cache_file.write_text(json.dumps(result, indent=2), encoding="utf-8")
                return result

    return {"ticker": ticker.upper(), "cik": None, "title": None}
