"""Error taxonomy and classification for TubePilot AI self-healing workflow.

Classifies arbitrary exceptions into 7 operational error classes:
1. TRANSIENT: Network glitch, short rate limit, timeout -> retry_same
2. PROVIDER_DOWN: 5xx, connection refused -> switch_provider
3. INVALID_KEY: 401/403, authentication failure -> switch_key
4. QUOTA_EXCEEDED: Daily exhaustion 429 -> reschedule_next_day
5. SCHEMA_FAIL: JSON validation/parse error -> re_prompt
6. CONTENT_FAIL: Verification gate failure -> fix_script
7. FATAL: Unrecoverable programming or system failure -> abort
"""

from __future__ import annotations

import enum
from typing import Optional


class ErrorClass(str, enum.Enum):
    """Categorization of errors for automated healing decisions."""
    TRANSIENT = "TRANSIENT"
    PROVIDER_DOWN = "PROVIDER_DOWN"
    INVALID_KEY = "INVALID_KEY"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    SCHEMA_FAIL = "SCHEMA_FAIL"
    CONTENT_FAIL = "CONTENT_FAIL"
    FATAL = "FATAL"


def classify_error(exc: Exception) -> ErrorClass:
    """Analyze an exception and return its ErrorClass.
    
    Inspects exception type, HTTP status codes, and error message patterns.
    """
    msg = str(exc).lower()
    exc_type = type(exc).__name__.lower()

    # 1. Invalid Key / Authentication failure
    if any(k in msg for k in ("401", "403", "unauthorized", "forbidden", "invalid api key", "invalid_api_key", "authentication failed")):
        return ErrorClass.INVALID_KEY

    # 2. Daily Quota Exceeded
    if any(q in msg for q in ("daily quota", "quota exceeded", "per day", "exceeded your current quota", "resource_exhausted")):
        if "rate limit" not in msg or "day" in msg:
            return ErrorClass.QUOTA_EXCEEDED

    # 3. Provider Down (5xx errors)
    if any(s in msg for s in ("500", "502", "503", "504", "bad gateway", "service unavailable", "connection refused", "internal server error")):
        return ErrorClass.PROVIDER_DOWN

    # 4. Schema Failure
    if any(s in msg for s in ("validation error", "jsondecodeerror", "schema validation", "failed schema")):
        return ErrorClass.SCHEMA_FAIL
    if "validationerror" in exc_type or "jsondecodeerror" in exc_type:
        return ErrorClass.SCHEMA_FAIL

    # 5. Content / Verification Failure
    if any(c in msg for c in ("verification failed", "content_fail", "hallucination detected", "forbidden phrase")):
        return ErrorClass.CONTENT_FAIL

    # 6. Transient / Rate Limit / Timeout
    if any(t in msg for t in ("429", "too many requests", "rate limit", "timeout", "timed out", "connecterror", "connection reset", "network")):
        return ErrorClass.TRANSIENT
    if "timeout" in exc_type or "connecterror" in exc_type:
        return ErrorClass.TRANSIENT

    # 7. Fallback to FATAL
    return ErrorClass.FATAL


__all__ = ["ErrorClass", "classify_error"]
