"""TubePilot AI — Pydantic v2 data models.

Re-exports every public model from the ``research``, ``script``, and
``verification`` submodules for convenient top-level imports::

    from models import ResearchBundle, VideoScript, VerificationReport
"""

from models.research import (
    CompetitorInfo,
    EntityInfo,
    FinancialData,
    MarginData,
    MarketData,
    ResearchBundle,
    SourceInfo,
    YTInsights,
)
from models.script import (
    ChapterInfo,
    SceneInfo,
    ScriptMeta,
    SEOData,
    ShortsScript,
    VideoScript,
)
from models.verification import (
    CheckResult,
    Issue,
    SuggestedFix,
    VerificationReport,
)

__all__ = [
    # research
    "EntityInfo",
    "MarginData",
    "FinancialData",
    "CompetitorInfo",
    "MarketData",
    "SourceInfo",
    "YTInsights",
    "ResearchBundle",
    # script
    "SceneInfo",
    "ChapterInfo",
    "SEOData",
    "ScriptMeta",
    "VideoScript",
    "ShortsScript",
    # verification
    "Issue",
    "CheckResult",
    "SuggestedFix",
    "VerificationReport",
]
