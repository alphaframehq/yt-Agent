"""TubePilot AI tool wrappers and external integrations."""

from tools.fal_ai import generate_fal_video
from tools.image_gen import generate_image, get_cached_image_path
from tools.llm_provider import LLMProvider, LLMResponse, call_with_fallback
from tools.pixabay import download_media, search_background_music, search_stock_videos
from tools.polly import format_ssml, synthesize_speech
from tools.replicate_tool import generate_flux_image
from tools.sec_edgar import get_company_facts, search_company_filings
from tools.secrets import (
    delete_secret,
    export_secrets,
    get_secret,
    import_secrets,
    list_secrets,
    mask_secret,
    rotate_secret,
    set_secret,
    test_secret,
)
from tools.tinyfish import get_topic_summary, search_fintech_context
from tools.yfinance_tool import get_competitor_comparison, get_ticker_financials
from tools.youtube_api import (
    delete_video,
    publish_public,
    research_topic,
    upload_private,
)

__all__ = [
    # Secrets
    "get_secret",
    "set_secret",
    "delete_secret",
    "list_secrets",
    "test_secret",
    "mask_secret",
    "rotate_secret",
    "export_secrets",
    "import_secrets",
    # LLM
    "LLMProvider",
    "LLMResponse",
    "call_with_fallback",
    # Image
    "generate_image",
    "get_cached_image_path",
    # SEC EDGAR
    "get_company_facts",
    "search_company_filings",
    # yfinance
    "get_ticker_financials",
    "get_competitor_comparison",
    # TinyFish
    "search_fintech_context",
    "get_topic_summary",
    # YouTube
    "research_topic",
    "upload_private",
    "publish_public",
    "delete_video",
    # Pixabay
    "search_stock_videos",
    "search_background_music",
    "download_media",
    # fal.ai
    "generate_fal_video",
    # Replicate
    "generate_flux_image",
    # Polly
    "synthesize_speech",
    "format_ssml",
]
