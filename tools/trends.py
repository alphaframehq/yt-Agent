"""Trend research for TubePilot AI — daily 1 long video + 3 Shorts.

NICHE (user correction, 26 Nov 2026): AI — anything new in AI.
"Jo bhi AI me new ho": new AI tools, projects, discoveries,
inventions, AI scams/fraud (ghotala), predictions, strategy,
formulas, ML / LLM training, advantages/disadvantages, clue leaks,
conspiracy — everything. NOT finance.

Runs BEFORE script generation for each topic. Fetches current
trends/angles for the topic + AI niche from free public RSS feeds —
no API keys, no paid services, no credentials:

1. Google News RSS — topic-specific recent headlines
   (``news.google.com/rss/search``).
2. Google Trends daily RSS — what is trending in the US today
   (``trends.google.com/trends/trendingsearches/daily/rss``),
   filtered to items that mention the topic / AI-niche keywords.
   NOTE (26 Nov 2026, verified from this sandbox): the Trends daily
   RSS endpoint currently returns HTTP 404 (both the RSS URL and
   the ``/trends/api/dailytrends`` JSON endpoint, US and IN geos) —
   Google appears to have removed it, or it is blocked here. The
   step degrades gracefully to Google News only (source 1) and
   records the 404 in ``report.errors``; if Trends comes back (e.g.
   from the GitHub Actions runner) it is picked up automatically
   with no code change.

Design contract (fail-soft, never fail-loud):

- ``fetch_trends()`` NEVER raises. Every source is fetched and
  parsed inside its own try/except; a dead network, a changed feed
  format, or an empty feed yields a ``TrendReport`` with fewer (or
  zero) angles plus a note in ``errors`` — the pipeline continues
  with the research bundle alone. Trend angles are editorial input
  (which hook / angle to lead with), never numeric facts, so an
  empty report can never fabricate a claim.
- Only the Python standard library is used (``urllib`` +
  ``xml.etree``), so this step adds no dependency and works in the
  GitHub Actions runner with no extra secrets.
- Angles are derived ONLY from real feed item titles/snippets.
  Nothing is invented: if a feed returns no items, no angle is
  produced from it.

Daily trending-topic picker (26 Nov 2026, ``pick_daily_trends``):

- Runs BEFORE dequeue each day (``scripts/daily_run.py``): fetches
  today's top AI headlines from the working keyless sources, scores
  them (recency + cross-source corroboration + AI relevance),
  clusters near-duplicate headlines across sources, and returns the
  strongest trending topic(s). A strong pick is PREFERRED over a
  stale queued topic; the queued 60 topics stay as the base queue
  and are the fallback. The picker never raises either.
- Source check, 26 Nov 2026 (live curl from this sandbox):
  WORKING — Google News RSS (HTTP 200, 100 items), Hacker News RSS
  (HTTP 200, 30 front-page items), Techmeme RSS (HTTP 200, 15
  items), Bing News RSS (HTTP 200, 12 items, AI query). FAILING —
  Google Trends daily RSS (HTTP 404, US; unchanged since the first
  check, still attempted by ``fetch_trends`` in case it comes back
  from the Actions runner) and YouTube trending (no public RSS/Atom
  feed exists; the trending page is JS-rendered HTML, not parseable
  as a feed — not used).
"""

from __future__ import annotations

import email.utils
import html
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

from loguru import logger
from pydantic import BaseModel, Field

__all__ = [
    "TrendItem",
    "TrendReport",
    "TrendingTopic",
    "DailyTrendPick",
    "derive_search_query",
    "fetch_trends",
    "parse_news_rss",
    "parse_trends_rss",
    "parse_frontpage_rss",
    "pick_daily_trends",
    "topic_from_headline",
    "trend_report_for_pick",
]

# Google News RSS search for the topic (public, no key).
_NEWS_RSS_URL = (
    "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
)
# Google Trends daily trending searches, US (public, no key).
_TRENDS_RSS_URL = (
    "https://trends.google.com/trends/trendingsearches/daily/rss?geo=US"
)
# Daily picker sources (26 Nov 2026: all verified HTTP 200, keyless).
# Hacker News front page and Techmeme are general tech feeds — items
# are AI-niche-filtered before use. Bing News RSS is query-based, so
# the AI query itself scopes it (same as Google News).
_HN_RSS_URL = "https://news.ycombinator.com/rss"
_TECHMEME_RSS_URL = "https://techmeme.com/feed.xml"
_BING_NEWS_RSS_URL = (
    "https://www.bing.com/news/search?q={query}&format=rss"
)
# Picker query: the AI niche as a whole, not one topic.
_PICKER_QUERY = "artificial intelligence"

_HTTP_TIMEOUT_SEC = 20.0
_USER_AGENT = "TubePilotAI/1.0 (trend research; +https://github.com/alphaframehq/yt-Agent)"

# AI-niche keywords (pillar ``ai_everything``): a daily-trending
# item is relevant to the day's output only if it touches the AI
# niche or the topic itself. Covers the user's brief: new tools,
# projects, discoveries, inventions, AI scams/fraud, predictions,
# strategy, formulas, ML/LLM training, pros/cons, leaks, conspiracy.
# Matched on WORD boundaries (see parse_trends_rss) — a bare
# substring match on "ai" would match "said", "again", "brain".
_NICHE_KEYWORDS = (
    "ai", "artificial intelligence", "llm", "gpt", "chatgpt",
    "openai", "anthropic", "claude", "gemini", "deepseek",
    "machine learning", "deep learning", "neural network",
    "generative ai", "genai", "agi", "ai agent", "agent",
    "robot", "robotics", "automation", "copilot", "midjourney",
    "stable diffusion", "nvidia", "gpu", "model training",
    "training", "fine-tuning", "fine tuning", "prompt",
    "ai scam", "scam", "fraud", "leak", "conspiracy",
    "prediction", "breakthrough", "discovery", "invention",
)

_MAX_NEWS_ITEMS = 10
_MAX_TREND_ITEMS = 10
_MAX_ANGLES = 5

# Daily picker tuning (26 Nov 2026). An item scores
# recency (0–1) + AI relevance (0.5–1); a cluster scores its best
# item + 0.5 per extra distinct source + 0.1 per extra item. A
# cluster needs >= _MIN_PICK_SCORE to be a pick: a single fresh,
# strongly-AI item scores 2.0 and passes; a single stale, weakly-AI
# item scores ~0.5 and does not.
_MAX_PICK_ITEMS_PER_SOURCE = 15
_MAX_PICK_TOPICS = 3
_MIN_PICK_SCORE = 1.5
_CLUSTER_SOURCE_BONUS = 0.5
_CLUSTER_ITEM_BONUS = 0.1

# Strong AI signals: naming a known AI company/product/lab, or one
# of the channel's core news angles (leak / scam / breakthrough).
# Used only to rank already-niche-filtered items, never to admit an
# off-niche item.
_STRONG_SIGNALS = (
    "openai", "anthropic", "deepmind", "google deepmind", "xai",
    "mistral", "hugging face", "huggingface", "chatgpt", "gpt-",
    "gemini", "claude", "copilot", "grok", "llama", "midjourney",
    "dall-e", "dalle", "sora", "runway", "perplexity", "cursor",
    "elevenlabs", "suno", "kling", "veo", "imagen",
    "stable diffusion", "cohere", "nvidia", "deepseek",
    "ai agent", "ai scam", "ai fraud", "ai leak", "ai breakthrough",
    "breakthrough", "leak", "scam", "fraud", "deepfake",
)

# Generic words ignored when clustering headlines by shared content
# words (a cluster needs >= 2 shared content words).
_CLUSTER_STOPWORDS = frozenset(
    """
    about after against among being between could should would
    their there these those under while with from into over
    says said say new today daily report reports reveals
    launches launch unveils announces announced company
    """.split()
)


class TrendItem(BaseModel):
    """One real trending item, traced to its source feed."""

    title: str
    source: str  # "google_news" | "google_trends_daily" | "hacker_news" | "techmeme" | "bing_news"
    url: str = ""
    snippet: str = ""
    # Epoch seconds from the feed's <pubDate>, 0 when the feed gives
    # no (parseable) date. Drives the picker's recency score.
    published_at: int = 0


class TrendingTopic(BaseModel):
    """One picked daily trending topic.

    ``topic`` is the cleaned headline of the cluster's best item —
    that string is what gets enqueued as the day's topic. ``score``
    is the cluster score (best item + cross-source bonus).
    ``items`` are the corroborating headlines across sources.
    """

    topic: str
    score: float = 0.0
    sources: List[str] = Field(default_factory=list)
    items: List[TrendItem] = Field(default_factory=list)
    angles: List[str] = Field(default_factory=list)


class DailyTrendPick(BaseModel):
    """Result of the daily trending-topic picker.

    ``topics`` are ranked strongest-first (max 3); empty when no
    source yielded a cluster above the pick threshold. ``sources``
    lists the feeds that returned usable AI items today; ``errors``
    records the feeds that failed or yielded nothing.
    """

    topics: List[TrendingTopic] = Field(default_factory=list)
    sources: List[str] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)
    fetched_at: int = Field(default_factory=lambda: int(time.time()))

    @property
    def has_pick(self) -> bool:
        return bool(self.topics)

    @property
    def best_topic(self) -> Optional[TrendingTopic]:
        return self.topics[0] if self.topics else None


class TrendReport(BaseModel):
    """Current-trend angles for one topic + niche.

    ``angles`` are short editorial hooks (e.g. "Why everyone's
    talking about X after today's Y") derived from real feed items;
    ``items`` are the underlying items with URLs so the run log /
    Sheet record shows exactly where each angle came from.
    """

    topic: str
    pillar: str = "ai_everything"
    angles: List[str] = Field(default_factory=list)
    items: List[TrendItem] = Field(default_factory=list)
    sources: List[str] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)
    fetched_at: int = Field(default_factory=lambda: int(time.time()))

    @property
    def has_trends(self) -> bool:
        return bool(self.angles)


def _clean(text: Optional[str]) -> str:
    """Strip HTML tags/entities/whitespace from a feed text field."""
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_pubdate(text: Optional[str]) -> int:
    """Parse an RSS <pubDate> to epoch seconds; 0 when absent/bad."""
    if not text:
        return 0
    try:
        dt = email.utils.parsedate_to_datetime(text.strip())
        if dt is None:
            return 0
        return int(dt.timestamp())
    except (TypeError, ValueError, OverflowError):
        return 0


def _parse_rss_items(xml_bytes: bytes) -> List[Dict[str, Any]]:
    """Parse an RSS 2.0 document into [{title, link, description, published_at}]."""
    root = ET.fromstring(xml_bytes)
    items: List[Dict[str, Any]] = []
    for item in root.iter("item"):
        title = _clean(item.findtext("title"))
        if not title:
            continue
        items.append(
            {
                "title": title,
                "link": _clean(item.findtext("link")),
                "description": _clean(item.findtext("description")),
                "published_at": _parse_pubdate(item.findtext("pubDate")),
            }
        )
    return items


def parse_news_rss(
    xml_bytes: bytes,
    max_items: int = _MAX_NEWS_ITEMS,
    source: str = "google_news",
) -> List[TrendItem]:
    """Parse a query-scoped news RSS (Google News / Bing News) into TrendItems.

    The feed query itself scopes the items (e.g. "artificial
    intelligence"), so no extra niche filter is applied here.
    """
    return [
        TrendItem(
            title=it["title"],
            source=source,
            url=it["link"],
            snippet=it["description"],
            published_at=it["published_at"],
        )
        for it in _parse_rss_items(xml_bytes)[:max_items]
    ]


def _is_niche_relevant(text: str, topic: Optional[str] = None) -> bool:
    """True when *text* touches the topic or the AI niche.

    Matching is on word boundaries (``\\bkeyword\\b``): short
    keywords such as "ai", "gpt" or "leak" must appear as whole words
    or phrases — a substring match would make "ai" match "said" and
    "brain" and let every trending item through.
    """
    haystack = text.lower()
    if topic:
        topic_words = {w.lower() for w in re.split(r"\W+", topic) if len(w) > 3}
        if topic_words & set(re.split(r"\W+", haystack)):
            return True
    return any(
        re.search(r"\b" + re.escape(kw) + r"\b", haystack)
        for kw in _NICHE_KEYWORDS
    )


def parse_frontpage_rss(
    xml_bytes: bytes,
    source: str,
    topic: Optional[str] = None,
    max_items: int = _MAX_PICK_ITEMS_PER_SOURCE,
) -> List[TrendItem]:
    """Parse a general front-page feed (Hacker News, Techmeme).

    Unlike the query-scoped news feeds, these carry all of tech, so
    every item is AI-niche-filtered (and optionally topic-matched)
    before it is kept.
    """
    kept: List[TrendItem] = []
    for it in _parse_rss_items(xml_bytes):
        if not _is_niche_relevant(f"{it['title']} {it['description']}", topic):
            continue
        kept.append(
            TrendItem(
                title=it["title"],
                source=source,
                url=it["link"],
                snippet=it["description"],
                published_at=it["published_at"],
            )
        )
        if len(kept) >= max_items:
            break
    return kept


def parse_trends_rss(
    xml_bytes: bytes, topic: str, max_items: int = _MAX_TREND_ITEMS
) -> List[TrendItem]:
    """Parse Google Trends daily RSS, keeping only items relevant to
    the topic or the AI niche.

    Matching is on word boundaries (``\\bkeyword\\b``): short
    keywords such as "ai", "gpt" or "leak" must appear as whole words
    or phrases — a substring match would make "ai" match "said" and
    "brain" and let every trending item through.
    """
    topic_words = {w.lower() for w in re.split(r"\W+", topic) if len(w) > 3}
    kept: List[TrendItem] = []
    for it in _parse_rss_items(xml_bytes):
        haystack = f"{it['title']} {it['description']}".lower()
        haystack_words = set(re.split(r"\W+", haystack))
        if topic_words & haystack_words:
            relevant = True
        else:
            relevant = _is_niche_relevant(haystack)
        if relevant:
            kept.append(
                TrendItem(
                    title=it["title"],
                    source="google_trends_daily",
                    url=it["link"],
                    snippet=it["description"],
                    published_at=it["published_at"],
                )
            )
        if len(kept) >= max_items:
            break
    return kept


def _angles_from_items(items: List[TrendItem], topic: str) -> List[str]:
    """Turn feed items into short editorial angles for the topic.

    Angles quote/paraphrase the real headline only — e.g. a news
    headline "X launches Y" becomes "What X's Y launch means for
    <topic>". No angle is produced for an item that does not exist.
    """
    angles: List[str] = []
    for it in items:
        title = it.title.strip()
        if not title:
            continue
        angle = f"Trending now: {title} — what it means for {topic}"
        if angle not in angles:
            angles.append(angle)
        if len(angles) >= _MAX_ANGLES:
            break
    return angles


def _fetch_url(url: str, opener: Any = None) -> bytes:
    """GET *url* with a timeout. *opener* is injectable for tests."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    if opener is not None:
        resp = opener(req, timeout=_HTTP_TIMEOUT_SEC)
    else:
        resp = urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SEC)
    try:
        return resp.read()
    finally:
        try:
            resp.close()
        except Exception:
            pass


# Search-query derivation (26 Nov 2026, run #15): a queued topic is a
# human sentence, often Hindi-mixed ("LLM Training Kaise Hoti Hai?
# Pretraining Se RLHF Tak — Simple Hindi Me"), and a news feed queried
# with the raw sentence returns nothing — Google News RSS (en-US) has
# no match for the Hindi question framing, so the run #15 RSS fallback
# came back with 0 items. The query feeds only need the content
# keywords: strip question/framework words (English + Roman Hindi)
# and single letters, keep up to 6 content tokens in original order
# and casing. Falls back to the AI-niche query when nothing survives.
_QUERY_STOPWORDS = frozenset(
    """
    how what why when where which who whom whose is are was were be
    been being do does did can could should would will shall may
    might the a an in on at to for of and or from with by about as
    into over under per via vs versus top best new latest simple
    hindi english explain explained explains guide tutorial basics
    learn learning understanding introduction intro complete full
    step steps kaise kya kyon kyun kab kahan kaun kis ka ki ke se
    tak me mein hai hain ho hoti hota hote raha rahe rahi aur ya par
    bhi jo ye yeh woh us is in ko ne na nahi ek do teen aaj kal
    samajh samjho batao janiye dekho
    """.split()
)
_MAX_QUERY_WORDS = 6


def derive_search_query(topic: str) -> str:
    """Short English keyword query from a human topic sentence.

    "LLM Training Kaise Hoti Hai? Pretraining Se RLHF Tak — Simple
    Hindi Me" -> "LLM Training Pretraining RLHF". Pure Hindi/empty
    topics degrade to the AI-niche query, never to "".
    """
    tokens: List[str] = []
    for token in re.split(r"[^A-Za-z0-9]+", topic or ""):
        if len(token) < 2:
            continue
        if token.lower() in _QUERY_STOPWORDS:
            continue
        if token in tokens:  # keep first occurrence, preserve order
            continue
        tokens.append(token)
        if len(tokens) >= _MAX_QUERY_WORDS:
            break
    return " ".join(tokens) if tokens else _PICKER_QUERY


def fetch_trends(
    topic: str,
    pillar: str = "ai_everything",
    opener: Any = None,
) -> TrendReport:
    """Fetch current trends/angles for *topic* + niche.

    Never raises: each source is attempted independently and any
    failure is recorded in ``report.errors`` while the other sources
    still proceed. Run #15 lesson: the fallback used to try only
    Google News (raw topic as query — 0 items for a Hindi-mixed
    question) and the known-dead Google Trends RSS, while the
    working Hacker News / Techmeme / Bing News feeds were only used
    by the daily picker, never here. All five sources are now
    attempted, and the query-scoped feeds (Google News, Bing News)
    use the derived keyword query (see ``derive_search_query``).
    """
    items: List[TrendItem] = []
    sources: List[str] = []
    errors: List[str] = []

    query = urllib.parse.quote_plus(derive_search_query(topic))

    # Source 1: topic-specific recent news (keyword query, not the
    # raw Hindi-mixed question — run #15 returned 0 items on it).
    try:
        news_xml = _fetch_url(_NEWS_RSS_URL.format(query=query), opener=opener)
        news_items = parse_news_rss(news_xml)
        if news_items:
            items.extend(news_items)
            sources.append("google_news")
        else:
            errors.append("google_news: feed returned no items")
    except Exception as exc:  # noqa: BLE001 — fail-soft by design
        errors.append(f"google_news: {exc}")

    # Source 2: US daily trending searches, niche-filtered.
    # Known 404 since 26 Nov 2026 — still attempted in case it comes
    # back from the Actions runner; never relied on.
    try:
        trends_xml = _fetch_url(_TRENDS_RSS_URL, opener=opener)
        trend_items = parse_trends_rss(trends_xml, topic)
        if trend_items:
            items.extend(trend_items)
            sources.append("google_trends_daily")
        else:
            errors.append("google_trends_daily: no niche-relevant items today")
    except Exception as exc:  # noqa: BLE001 — fail-soft by design
        errors.append(f"google_trends_daily: {exc}")

    # Source 3: Hacker News front page, AI-niche/topic-filtered.
    try:
        hn_xml = _fetch_url(_HN_RSS_URL, opener=opener)
        hn_items = parse_frontpage_rss(hn_xml, source="hacker_news", topic=topic)
        if hn_items:
            items.extend(hn_items)
            sources.append("hacker_news")
        else:
            errors.append("hacker_news: feed returned no AI-relevant items")
    except Exception as exc:  # noqa: BLE001 — fail-soft by design
        errors.append(f"hacker_news: {exc}")

    # Source 4: Techmeme, AI-niche/topic-filtered.
    try:
        tm_xml = _fetch_url(_TECHMEME_RSS_URL, opener=opener)
        tm_items = parse_frontpage_rss(tm_xml, source="techmeme", topic=topic)
        if tm_items:
            items.extend(tm_items)
            sources.append("techmeme")
        else:
            errors.append("techmeme: feed returned no AI-relevant items")
    except Exception as exc:  # noqa: BLE001 — fail-soft by design
        errors.append(f"techmeme: {exc}")

    # Source 5: Bing News, keyword-query-scoped like Google News.
    try:
        bing_xml = _fetch_url(_BING_NEWS_RSS_URL.format(query=query), opener=opener)
        bing_items = parse_news_rss(bing_xml, source="bing_news")
        if bing_items:
            items.extend(bing_items)
            sources.append("bing_news")
        else:
            errors.append("bing_news: feed returned no items")
    except Exception as exc:  # noqa: BLE001 — fail-soft by design
        errors.append(f"bing_news: {exc}")

    report = TrendReport(
        topic=topic,
        pillar=pillar,
        angles=_angles_from_items(items, topic),
        items=items,
        sources=sources,
        errors=errors,
    )
    logger.info(
        f"Trend research for '{topic}' (query: '{derive_search_query(topic)}'): "
        f"{len(report.angles)} angles from "
        f"{len(report.items)} items (sources: {sources or 'none'}; "
        f"errors: {errors or 'none'})"
    )
    for angle in report.angles:
        logger.info(f"  trend angle: {angle}")
    return report


# ---------------------------------------------------------------------------
# Daily trending-topic picker (runs BEFORE dequeue — see
# scripts/daily_run._prefer_trending_topic).
# ---------------------------------------------------------------------------

def topic_from_headline(headline: str) -> str:
    """Turn a feed headline into a queueable topic string.

    Google News headlines append " - <Publisher>" (e.g. "… - The
    New York Times"). That tail is dropped only when it looks like
    an outlet name — every word title-cased and the remaining
    headline still >= 15 chars — so a headline such as "AI - the
    future is now" is left untouched. Capped at 140 chars so the
    topic stays a usable research query.
    """
    text = _clean(headline)
    if " - " in text:
        head, _, tail = text.rpartition(" - ")
        tail_words = tail.split()
        if (
            head
            and len(head) >= 15
            and tail_words
            and len(tail_words) <= 5
            and all(w[:1].isupper() for w in tail_words)
        ):
            text = head
    return text[:140].strip()


def _recency_score(published_at: int, now: int) -> float:
    """Freshness 0–1 from a feed <pubDate> (0 = undated/stale)."""
    if not published_at:
        return 0.25  # undated items: neutral, never top-ranked alone
    age_h = (now - published_at) / 3600.0
    if age_h < 0:
        return 1.0  # future-dated (clock skew): treat as fresh
    if age_h <= 12:
        return 1.0
    if age_h <= 24:
        return 0.75
    if age_h <= 48:
        return 0.5
    if age_h <= 72:
        return 0.25
    return 0.0


def _relevance_score(item: TrendItem) -> float:
    """AI relevance 0.5–1 for an already niche-filtered item.

    1.0 when the headline/snippet names a known AI company/product
    or one of the channel's core news angles (leak / scam /
    breakthrough); 0.5 otherwise. Ranking only — admission is
    decided by the niche filter, never here.
    """
    haystack = f"{item.title} {item.snippet}".lower()
    return 1.0 if any(sig in haystack for sig in _STRONG_SIGNALS) else 0.5


def _score_item(item: TrendItem, now: int) -> float:
    """Item score: recency (0–1) + AI relevance (0.5–1)."""
    return _recency_score(item.published_at, now) + _relevance_score(item)


def _content_words(title: str) -> frozenset:
    """Content words of a headline (len >= 4, stopwords removed)."""
    words = {w for w in re.split(r"\W+", title.lower()) if len(w) >= 4}
    return frozenset(words - _CLUSTER_STOPWORDS)


def _cluster_items(items: List[TrendItem], now: int) -> List[Any]:
    """Group items about the same story (shared content words).

    Greedy, best-first: items are scored, then each joins the first
    existing cluster sharing >= 2 content words with any member, or
    starts a new one. Returns ``[(cluster_score, [items, ...]), …]``
    strongest-first, where the cluster score is its best item plus
    0.5 per extra distinct source and 0.1 per extra item — the same
    story reported by several sources outranks a single-source one.
    """
    scored = sorted(items, key=lambda it: _score_item(it, now), reverse=True)
    clusters: List[Dict[str, Any]] = []
    for item in scored:
        words = _content_words(item.title)
        placed = False
        for cluster in clusters:
            if len(words & cluster["words"]) >= 2:
                cluster["items"].append(item)
                cluster["words"] |= words
                placed = True
                break
        if not placed:
            clusters.append({"words": set(words), "items": [item]})
    ranked: List[Any] = []
    for cluster in clusters:
        citems = cluster["items"]
        best = max(_score_item(it, now) for it in citems)
        sources = {it.source for it in citems}
        score = (
            best
            + _CLUSTER_SOURCE_BONUS * (len(sources) - 1)
            + _CLUSTER_ITEM_BONUS * (len(citems) - 1)
        )
        ranked.append((score, citems))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    return ranked


def pick_daily_trends(
    opener: Any = None,
    max_topics: int = _MAX_PICK_TOPICS,
    now: Optional[int] = None,
) -> DailyTrendPick:
    """Pick today's strongest trending AI topic(s) from free RSS feeds.

    Fetches today's AI headlines from the working keyless sources
    (Google News AI query, Hacker News front page, Techmeme, Bing
    News AI query), AI-filters the general feeds, scores items
    (recency + AI relevance), clusters near-duplicate headlines
    across sources, and returns the top clusters above
    ``_MIN_PICK_SCORE`` as ``TrendingTopic`` entries.

    Never raises: each source is attempted independently and any
    failure is recorded in ``pick.errors`` while the other sources
    still proceed. An empty pick (no clusters above threshold) means
    "no strong trend today" — the caller falls back to the queue.
    """
    now = now if now is not None else int(time.time())
    items: List[TrendItem] = []
    sources: List[str] = []
    errors: List[str] = []

    query = urllib.parse.quote_plus(_PICKER_QUERY)
    fetch_plan = [
        (
            _NEWS_RSS_URL.format(query=query),
            "google_news",
            lambda xml: parse_news_rss(xml, max_items=_MAX_PICK_ITEMS_PER_SOURCE),
        ),
        (
            _HN_RSS_URL,
            "hacker_news",
            lambda xml: parse_frontpage_rss(xml, source="hacker_news"),
        ),
        (
            _TECHMEME_RSS_URL,
            "techmeme",
            lambda xml: parse_frontpage_rss(xml, source="techmeme"),
        ),
        (
            _BING_NEWS_RSS_URL.format(query=query),
            "bing_news",
            lambda xml: parse_news_rss(
                xml, max_items=_MAX_PICK_ITEMS_PER_SOURCE, source="bing_news"
            ),
        ),
    ]
    for url, name, parse in fetch_plan:
        try:
            parsed = parse(_fetch_url(url, opener=opener))
        except Exception as exc:  # noqa: BLE001 — fail-soft by design
            errors.append(f"{name}: {exc}")
            continue
        if parsed:
            items.extend(parsed)
            sources.append(name)
        else:
            errors.append(f"{name}: feed returned no AI-relevant items")

    topics: List[TrendingTopic] = []
    for score, citems in _cluster_items(items, now):
        if score < _MIN_PICK_SCORE:
            break  # clusters are ranked strongest-first
        best_item = max(citems, key=lambda it: _score_item(it, now))
        topic = topic_from_headline(best_item.title)
        if not topic:
            continue
        topics.append(
            TrendingTopic(
                topic=topic,
                score=round(score, 2),
                sources=sorted({it.source for it in citems}),
                items=list(citems),
                angles=_angles_from_items(citems, topic),
            )
        )
        if len(topics) >= max_topics:
            break

    pick = DailyTrendPick(
        topics=topics, sources=sources, errors=errors, fetched_at=now
    )
    logger.info(
        f"Daily trend pick: {len(topics)} topic(s) from {len(items)} items "
        f"(sources: {sources or 'none'}; errors: {errors or 'none'})"
    )
    for t in topics:
        logger.info(f"  trending pick: '{t.topic}' (score {t.score}; sources: {t.sources})")
    return pick


def trend_report_for_pick(
    pick: DailyTrendPick, topic: Optional[str] = None
) -> TrendReport:
    """Build a TrendReport from a picker result, for the yt_trends tab.

    The Sheet row is keyed by the picked topic, so its angles/sources
    are that topic's own cluster (not the whole day's list). The full
    item list (with URLs) stays in the run log — ``yt_trends`` stores
    angles + sources only (see ``db/sheets.save_trend_report``).
    """
    best = pick.best_topic
    chosen = topic or (best.topic if best else "")
    if (
        best is not None
        and chosen.strip().lower() == best.topic.strip().lower()
    ):
        angles, items, sources = best.angles, best.items, best.sources
    else:
        angles = [t.angles[0] for t in pick.topics if t.angles]
        items = [it for t in pick.topics for it in t.items]
        sources = list(pick.sources)
    return TrendReport(
        topic=chosen,
        pillar="ai_everything",
        angles=angles,
        items=items,
        sources=sources,
        errors=list(pick.errors),
        fetched_at=pick.fetched_at,
    )
