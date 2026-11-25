# PRODUCTION PATCH (local only, 2026-11-25) — yt-Agent audit P0 #3.
# search_fintech_context() now RAISES on missing TINYFISH_API_KEY, API error,
# non-200 response, or zero articles — instead of returning fabricated
# articles with fake URLs (alphapilot.io/research/..., fintechinsights.io),
# fake sources and fake 2024-Q3/Q4 dates. The fabricated fallback and the
# caching of fabricated articles are removed; only real API results are
# cached. Stale cache entries holding fabricated articles are dropped.
# get_topic_summary() is unchanged in structure — it now propagates the raise
# instead of citing fabricated articles.
# Source: alphaframehq/yt-Agent @ c39f42541b8e784dcf41c09583dc57227be07667.
# NOT pushed to GitHub. No secrets in this file — env-var placeholder only.
"""TinyFish and web intelligence tool for TubePilot AI.

Gathers recent fintech trends, architecture deep-dives, breaking news,
and technical explainers to supply context for script hooks and analysis.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List
import httpx
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from tools.secrets import get_secret

__all__ = [
    "search_fintech_context",
    "get_topic_summary",
]

CACHE_DIR = Path("cache/tinyfish")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

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


@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=2, max=5))
def search_fintech_context(topic: str, max_results: int = 5) -> List[Dict[str, Any]]:
    """Fetch recent news, blog posts, and technical writeups on a fintech topic.

    Raises ValueError if TINYFISH_API_KEY is missing, and RuntimeError on API
    error, non-200 response, or zero articles returned. Never fabricates
    articles. Only real API results are cached.
    """
    cache_file = _get_cache_path(f"search:{topic}")

    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            # Drop any fabricated articles cached by the pre-patch fallback
            # instead of serving them as if they were real research.
            real = [a for a in cached if not _is_fabricated_article(a)]
            if real:
                return real
            logger.warning(
                "TinyFish cache contained only fabricated articles; re-querying API."
            )
        except Exception:
            pass

    api_key = get_secret("TINYFISH_API_KEY")
    if not api_key:
        # PRODUCTION PATCH (P0 #3): missing key must fail loudly, not fall
        # through to fabricated articles.
        raise ValueError("TINYFISH_API_KEY is required for TinyFish search")

    url = f"https://api.tinyfish.io/v1/search"
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(url, headers=headers, json={"query": topic, "limit": max_results})
            if resp.status_code != 200:
                raise RuntimeError(
                    f"TinyFish API returned HTTP {resp.status_code} for topic '{topic}'"
                )
            data = resp.json()
            results = []
            for item in data.get("articles", []):
                results.append({
                    "title": item.get("title", ""),
                    "snippet": item.get("summary", ""),
                    "url": item.get("url", ""),
                    "source": item.get("publisher", "TinyFish News"),
                    "published_at": item.get("published_at", ""),
                })
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        logger.error(f"TinyFish API query failed: {e}")
        raise RuntimeError(f"TinyFish API query failed for topic '{topic}': {e}") from e

    if not results:
        # PRODUCTION PATCH (P0 #3): zero real articles is a failure, not a
        # cue to invent articles.
        raise RuntimeError(f"TinyFish returned zero articles for topic '{topic}'")

    cache_file.write_text(json.dumps(results, indent=2), encoding="utf-8")
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
