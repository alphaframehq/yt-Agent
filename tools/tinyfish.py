# PRODUCTION PATCH (local only, 2026-11-25) — yt-Agent audit P0 #3.
# search_fintech_context() RAISED on missing TINYFISH_API_KEY, API error,
# non-200 response, or zero articles — instead of returning fabricated
# articles with fake URLs (alphapilot.io/research/..., fintechinsights.io),
# fake sources and fake 2024-Q3/Q4 dates. The fabricated fallback and the
# caching of fabricated articles are removed; only real API results are
# cached. Stale cache entries holding fabricated articles are dropped.
# get_topic_summary() is unchanged in structure — it now propagates the raise
# instead of citing fabricated articles.
# UPDATE (26 Nov 2026, run #14): the TinySearch host moved —
# api.tinyfish.io is NXDOMAIN, the live endpoint is
# api.search.tinyfish.ai (GET, X-API-Key). A dead TinySearch no longer
# hard-fails the run: search_fintech_context() falls back to real
# keyless RSS articles (tools/trends.py) and raises only when BOTH
# sources yield nothing. The no-fabrication rule is unchanged.
# Source: alphaframehq/yt-Agent @ c39f42541b8e784dcf41c09583dc57227be07667.
# Applied via PR #1 (branch fix/production-mocks), 2026-11-25. No secrets in this file — env-var placeholder only.
"""TinyFish and web intelligence tool for TubePilot AI.

Gathers recent fintech trends, architecture deep-dives, breaking news,
and technical explainers to supply context for script hooks and analysis.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import httpx
from loguru import logger
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from tools.secrets import get_secret

__all__ = [
    "search_fintech_context",
    "get_topic_summary",
]

CACHE_DIR = Path("cache/tinyfish")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Cache TTL (27 Nov 2026): a cached TinySearch result older than 24h
# must be re-fetched, never served forever — news goes stale and the
# script would cite yesterday's news as today's.
CACHE_TTL_SEC = 24 * 3600

# Max article age (27 Nov 2026): the script must not cite month-old
# news as current. Articles with a parseable publish date older than
# 7 days are dropped from TinySearch and RSS results. Undated (or
# unparseable-date) articles are kept — their age cannot be verified
# and dropping them would fail runs on dateless feeds.
MAX_ARTICLE_AGE_DAYS = 7

# URL fragments used by the pre-patch fabricated-article fallback. Any cached
# article whose URL contains one of these was invented, never retrieved.
_FABRICATED_ARTICLE_URL_FRAGMENTS = (
    "alphapilot.io/research/",
    "fintechinsights.io/deep-dive/",
)


def _get_cache_path(query: str) -> Path:
    h = hashlib.md5(query.encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{h}.json"


def _is_fabricated_article(article: Dict[str, Any]) -> bool:
    """True if the article came from the removed fabricated fallback."""
    url = str(article.get("url", ""))
    return any(fragment in url for fragment in _FABRICATED_ARTICLE_URL_FRAGMENTS)


# TinySearch host move (verified 26 Nov 2026 — run #14 root cause):
# the old https://api.tinyfish.io/v1/search is DEAD. Public DNS
# (8.8.8.8, 1.1.1.1) returns NO records for api.tinyfish.io, so the
# GitHub Actions runner failed with gaierror EAI_NONAME ("No address
# associated with hostname"). The vendor moved Search to
# https://api.search.tinyfish.ai (GET, X-API-Key header) — see
# https://docs.tinyfish.ai/search-api (crawled 26 Nov 2026) and the
# official tinyfish PyPI SDK. The old host is gone, not flaky: do not
# "retry harder" against it.
# Fallback (26 Nov 2026): TinySearch is a single point of failure for
# the research node. When it is unreachable / errors / returns zero
# articles (or the key is missing), research falls back to the
# already-working keyless RSS feeds (tools/trends.py — Google News RSS,
# Hacker News, Techmeme, Bing News), which return REAL articles with
# real titles/URLs. The fallback never fabricates: if RSS also yields
# nothing, search_fintech_context raises and the run fails loudly —
# correctly — instead of scripting from invented facts.
# UPDATE (26 Nov 2026, run #15): a per-domain HTTP error used to abort
# the news->web loop entirely — TinySearch returned HTTP 400 for
# domain_type=news on the long Hindi-mixed topic and web was never
# tried. _tinysearch_query() now catches the error per domain type and
# falls through to web before giving up and hitting the RSS fallback.
TINYSEARCH_BASE_URL = "https://api.search.tinyfish.ai"

# RSS source labels for fallback articles (feed ids in tools/trends.py).
_RSS_SOURCE_LABELS = {
    "google_news": "Google News",
    "google_trends_daily": "Google Trends",
    "hacker_news": "Hacker News",
    "techmeme": "TechMeme",
    "bing_news": "Bing News",
}


def _epoch_to_iso(epoch: int) -> str:
    """RSS pubDate epoch -> ISO-8601 UTC string ('' when undated)."""
    if not epoch:
        return ""
    try:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        return ""


def _parse_published_at(value: Any) -> Optional[datetime]:
    """Parse a publish date to an aware UTC datetime.

    Accepts ISO-8601 strings (with or without a trailing ``Z``),
    plain ``YYYY-MM-DD`` dates, and epoch seconds. Returns None when
    the article is undated or the date is unparseable — the caller
    treats that as "age unknown", never as stale.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (TypeError, ValueError, OverflowError):
            return None
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = datetime.strptime(raw, "%Y-%m-%d")
            except ValueError:
                return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    return None


def _filter_fresh(
    articles: List[Dict[str, Any]],
    max_age_days: int = MAX_ARTICLE_AGE_DAYS,
) -> List[Dict[str, Any]]:
    """Drop articles older than ``max_age_days``; undated kept."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    fresh = []
    for article in articles:
        published = _parse_published_at(article.get("published_at"))
        if published is not None and published < cutoff:
            logger.info(
                f"Dropping stale article ({published.date()}) older than "
                f"{max_age_days}d: {str(article.get('title', ''))[:80]}"
            )
            continue
        fresh.append(article)
    return fresh


def _tinysearch_get(
    api_key: str, topic: str, max_results: int, domain_type: str
) -> List[Dict[str, Any]]:
    """One TinySearch GET. Returns [] on zero results; raises on
    transport / HTTP / malformed-response failure."""
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(
                TINYSEARCH_BASE_URL,
                headers={"X-API-Key": api_key},
                params={"q": topic, "domain_type": domain_type, "language": "en"},
            )
            if resp.status_code != 200:
                raise RuntimeError(
                    f"TinySearch returned HTTP {resp.status_code} "
                    f"(domain_type={domain_type}) for topic '{topic}'"
                )
            try:
                data = resp.json()
            except ValueError as exc:
                raise RuntimeError(
                    f"TinySearch returned non-JSON response for topic '{topic}': {exc}"
                ) from exc
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        logger.error(f"TinySearch query failed: {e}")
        raise RuntimeError(f"TinySearch query failed for topic '{topic}': {e}") from e

    results = []
    for item in data.get("results", []) or []:
        if not isinstance(item, dict):
            continue
        results.append(
            {
                "title": item.get("title", "") or "",
                "snippet": item.get("snippet", "") or "",
                "url": item.get("url", "") or "",
                "source": item.get("publisher")
                or item.get("site_name")
                or "TinyFish",
                "published_at": item.get("published_at")
                or item.get("date")
                or item.get("published")
                or "",
            }
        )
    return results[:max_results]


@retry(
    retry=retry_if_exception_type(RuntimeError),
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=1, min=2, max=5),
    # Re-raise the original RuntimeError (not tenacity.RetryError) so
    # the caller in search_fintech_context() can catch it and fall back.
    reraise=True,
)
def _tinysearch_query(topic: str, max_results: int) -> List[Dict[str, Any]]:
    """TinySearch primary path: news articles first, then general web.

    Each domain type is tried independently: an HTTP error on one
    (run #15 — TinySearch returned HTTP 400 for domain_type=news on
    the long Hindi-mixed topic, which used to abort the loop before
    web was ever tried) falls through to the next domain type
    instead of failing the whole query. Only when BOTH domain types
    error or return zero articles does this raise RuntimeError
    (retried once by tenacity, then the caller falls back to RSS).

    Raises ValueError if TINYFISH_API_KEY is missing (not retried).
    """
    api_key = get_secret("TINYFISH_API_KEY")
    if not api_key:
        raise ValueError("TINYFISH_API_KEY is required for TinySearch")

    last_error: Exception | None = None
    for domain_type in ("news", "web"):
        try:
            articles = _tinysearch_get(api_key, topic, max_results, domain_type)
        except RuntimeError as exc:
            # A per-domain failure (e.g. HTTP 400 on news) must not
            # skip the other domain type — log and keep going.
            logger.warning(
                f"TinySearch {domain_type} failed for topic '{topic}' "
                f"({exc}); trying the next domain type."
            )
            last_error = exc
            continue
        # Freshness (27 Nov 2026): month-old news must not be cited
        # as current — drop stale articles per domain type, so a
        # stale "news" result falls through to "web" instead of
        # being served.
        articles = _filter_fresh(articles)
        if articles:
            return articles
    raise RuntimeError(
        f"TinySearch returned zero articles for topic '{topic}' "
        f"(news + web)"
        + (f"; last error: {last_error}" if last_error else "")
    )


def _search_rss_fallback(
    topic: str, max_results: int, opener: Any = None
) -> List[Dict[str, Any]]:
    """Keyless RSS fallback (tools/trends.py): real articles from the
    working public feeds. Never raises; [] means "no real articles"."""
    from tools.trends import fetch_trends

    report = fetch_trends(topic=topic, pillar="ai_everything", opener=opener)
    articles = []
    for item in report.items[:max_results]:
        articles.append(
            {
                "title": item.title,
                "snippet": item.snippet,
                "url": item.url,
                "source": _RSS_SOURCE_LABELS.get(item.source, item.source),
                "published_at": _epoch_to_iso(item.published_at),
            }
        )
    # Freshness (27 Nov 2026): same 7-day max age as TinySearch —
    # the script must not cite month-old RSS news as current.
    return _filter_fresh(articles)


def search_fintech_context(
    topic: str, max_results: int = 5, _opener: Any = None
) -> List[Dict[str, Any]]:
    """Fetch recent news, blog posts, and technical writeups on a topic.

    Primary: TinySearch (GET https://api.search.tinyfish.ai,
    X-API-Key). If it is unreachable, errors, returns zero articles,
    or the API key is missing, research falls back automatically to
    real keyless RSS articles (Google News / Hacker News / TechMeme /
    Bing News via tools/trends.py). Raises RuntimeError only when
    BOTH sources yield nothing — the run then fails loudly instead
    of scripting from invented facts.

    Only real TinySearch results are cached; RSS fallback results are
    re-fetched every run (cheap, keyless) so a recovered TinySearch
    is picked up again instead of serving a stale cached fallback.
    Never fabricates articles.

    ``_opener`` is test-only: an injectable URL opener passed through
    to the RSS fallback's ``fetch_trends``.
    """
    cache_file = _get_cache_path(f"search:{topic}")

    if cache_file.exists():
        try:
            # 24h TTL (27 Nov 2026): a stale cache must be re-fetched,
            # never served forever — news goes stale.
            if time.time() - cache_file.stat().st_mtime > CACHE_TTL_SEC:
                logger.info(
                    "TinyFish cache is older than 24h; re-fetching "
                    f"instead of serving it for topic '{topic}'."
                )
            else:
                cached = json.loads(cache_file.read_text(encoding="utf-8"))
                # Drop any fabricated articles cached by the pre-patch fallback
                # instead of serving them as if they were real research.
                real = [a for a in cached if not _is_fabricated_article(a)]
                # Same 7-day max age as fresh results — a pre-patch
                # cache entry could hold an already-stale article.
                real = _filter_fresh(real)
                if real:
                    return real
                logger.warning(
                    "TinyFish cache contained only fabricated or stale "
                    "articles; re-querying API."
                )
        except Exception:
            pass

    try:
        results = _tinysearch_query(topic, max_results)
        cache_file.write_text(json.dumps(results, indent=2), encoding="utf-8")
        return results
    except (ValueError, RuntimeError) as exc:
        # PRODUCTION PATCH (P0 #3) intent preserved — a dead TinySearch
        # must fail LOUDLY, never fall through to fabricated articles.
        # The loud failure is now the RSS fallback + this warning, not
        # a dead run: the fallback returns only real retrieved RSS
        # articles. If RSS also yields nothing, we raise below.
        logger.warning(
            f"TinySearch unavailable for topic '{topic}' ({exc}); "
            "falling back to keyless RSS research."
        )
        results = _search_rss_fallback(topic, max_results, opener=_opener)

    if not results:
        # Zero real articles from BOTH sources is a failure, not a cue
        # to invent articles.
        raise RuntimeError(
            f"No research articles retrieved for topic '{topic}': "
            "TinySearch and the RSS fallback both returned nothing."
        )
    return results


def get_topic_summary(topic: str) -> str:
    """Concatenate article snippets into a unified context block.

    Only cites real articles returned by search_fintech_context(); raises if
    no real articles can be retrieved (see P0 #3).
    """
    articles = search_fintech_context(topic)
    summary_lines = []
    for a in articles:
        summary_lines.append(f"[{a['source']}] {a['title']}: {a['snippet']} (Source: {a['url']})")
    return "\n".join(summary_lines)
