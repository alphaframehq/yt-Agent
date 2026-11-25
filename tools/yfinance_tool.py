# PRODUCTION PATCH (local only, 2026-11-25) — yt-Agent audit P0 #2.
# get_ticker_financials() now RAISES on any failure (missing yfinance lib,
# network error, invalid/unknown ticker, or empty data) instead of returning
# hardcoded invented financials ($1.0B/$1.25B revenue, margins 0.65/0.22/0.18,
# growth 0.25, market cap $25B). All invented numbers are removed.
# get_competitor_comparison() propagates that raise (it only ever forwarded
# get_ticker_financials() results). The real yfinance call path is intact.
# Stale cache entries holding the old mock payload are ignored and re-queried.
# Source: alphaframehq/yt-Agent @ c39f42541b8e784dcf41c09583dc57227be07667.
# Applied via PR #1 (branch fix/production-mocks), 2026-11-25. No secrets in this file.
"""yfinance integration tool for TubePilot AI.

Extracts real market and financial metrics:
- Historical revenues and growth rates
- Gross, operating, and net margins
- Competitor benchmarks
- Business segments and balance sheet highlights
All queries are cached locally to ensure reproducibility and prevent rate-limiting.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

__all__ = [
    "get_ticker_financials",
    "get_competitor_comparison",
]

CACHE_DIR = Path("cache/yfinance")
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _get_cache_path(key: str) -> Path:
    h = hashlib.md5(key.encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{h}.json"


def _is_legacy_mock_data(data: Dict[str, Any]) -> bool:
    """Detect the hardcoded mock payload the pre-patch code cached on failure.

    Exact signature: revenue 2023=$1.0B / 2024=$1.25B, margins 0.65/0.22/0.18,
    market cap $25B. No real company matches this exact combination, so a
    match is safe to treat as a stale mock and re-query.
    """
    try:
        return (
            data.get("revenue_by_year") == {"2023": 1000000000.0, "2024": 1250000000.0}
            and data.get("margins") == {"gross": 0.65, "operating": 0.22, "net": 0.18}
            and float(data.get("market_cap", 0.0) or 0.0) == 25000000000.0
        )
    except Exception:
        return False


@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=2, max=5))
def get_ticker_financials(ticker_symbol: str) -> Dict[str, Any]:
    """Retrieve financial statements, ratios, and margins for a ticker.

    Raises RuntimeError on any failure (missing yfinance lib, network error,
    invalid ticker, or no usable data returned). Never returns invented
    financials.
    """
    symbol = ticker_symbol.upper().strip()
    cache_file = _get_cache_path(f"financials:{symbol}")

    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if _is_legacy_mock_data(cached):
                logger.warning(
                    f"Ignoring legacy mock cache entry for {symbol}; re-querying yfinance."
                )
            else:
                return cached
        except Exception:
            pass

    logger.info(f"Querying yfinance for {symbol}")

    try:
        import yfinance as yf

        ticker = yf.Ticker(symbol)
        info = ticker.info or {}

        # 1. Margins
        gross_margin = float(info.get("grossMargins", 0.0) or 0.0)
        operating_margin = float(info.get("operatingMargins", 0.0) or 0.0)
        net_margin = float(info.get("profitMargins", 0.0) or 0.0)
        growth_rate = float(info.get("revenueGrowth", 0.0) or 0.0)

        # 2. Historical revenue by year
        revenue_by_year: Dict[str, float] = {}
        try:
            financials = ticker.financials
            if financials is not None and not financials.empty:
                if "Total Revenue" in financials.index:
                    rev_row = financials.loc["Total Revenue"]
                    for date_col, val in rev_row.items():
                        year_str = str(date_col)[:4]
                        revenue_by_year[year_str] = float(val)
        except Exception as e:
            logger.warning(f"Could not parse income statement dataframe for {symbol}: {e}")

        # If empty, fallback to info revenue
        if not revenue_by_year and "totalRevenue" in info and info["totalRevenue"]:
            revenue_by_year["TTM"] = float(info["totalRevenue"])

        # PRODUCTION PATCH (P0 #2): yfinance returns an empty info dict for
        # unknown/invalid tickers instead of raising. Treat "no usable data"
        # as a failure rather than caching/returning all-zero financials.
        if not revenue_by_year and not any(
            (gross_margin, operating_margin, net_margin)
        ):
            raise ValueError(
                f"yfinance returned no usable financial data for {symbol} "
                "(unknown ticker, delisted, or Yahoo Finance returned nothing)"
            )

        result = {
            "symbol": symbol,
            "company_name": info.get("longName") or info.get("shortName") or symbol,
            "currency": info.get("currency", "USD"),
            "revenue_by_year": revenue_by_year,
            "margins": {
                "gross": gross_margin,
                "operating": operating_margin,
                "net": net_margin,
            },
            "growth_rate": growth_rate,
            "market_cap": float(info.get("marketCap", 0.0) or 0.0),
            "free_cashflow": float(info.get("freeCashflow", 0.0) or 0.0),
            "source_url": f"https://finance.yahoo.com/quote/{symbol}",
        }

        cache_file.write_text(json.dumps(result, indent=2), encoding="utf-8")
        return result

    except Exception as e:
        logger.error(f"yfinance lookup failed for {symbol}: {e}")
        # PRODUCTION PATCH (P0 #2): never return invented financials on
        # failure. Callers (e.g. agents/research.py) must fail loudly.
        raise RuntimeError(f"yfinance lookup failed for {symbol}: {e}") from e


def get_competitor_comparison(tickers: List[str]) -> List[Dict[str, Any]]:
    """Compare multiple competitors across key valuation and efficiency metrics.

    Raises if any ticker's lookup fails (via get_ticker_financials) — a
    comparison built on a failed lookup must not silently contain invented
    numbers for that ticker.
    """
    results = []
    for t in tickers:
        data = get_ticker_financials(t)
        results.append({
            "symbol": data["symbol"],
            "name": data["company_name"],
            "growth_rate": data["growth_rate"],
            "gross_margin": data["margins"]["gross"],
            "operating_margin": data["margins"]["operating"],
            "market_cap": data["market_cap"],
        })
    return results
