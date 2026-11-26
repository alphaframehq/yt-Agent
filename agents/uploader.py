# LOCAL PRODUCTION PATCH — 2026-11-25 — Applied via PR #1 (branch fix/production-mocks), 2026-11-25.
# Source: alphaframehq/yt-Agent @ main c39f42541b8e784dcf41c09583dc57227be07667
# Fixes audit P0 #12 (task slice #13): upload_and_register() now REQUIRES
# a passing VerificationReport (real score from report.confidence, never
# the old hardcoded 1.0), refuses on a fail verdict, rejects files below
# a minimum size/duration or failing an ffprobe probe, and does NOT purge
# local media until the YouTube copy is verified via videos.list
# (tools.youtube_api.verify_video_exists) AND the Google Sheets record is
# saved. Empty/missing thumbnails warn (no thumbnail) or raise (supplied
# but missing/empty) instead of being silently recorded as "".
# BREAKING CHANGE: signature changed — verification_report is a required
# argument. Callers are updated: orchestration/graph.py node_finalize
# now calls the uploader and passes the real VerificationReport from
# node_verify (raises if missing or not "pass"); never fabricate a
# report. run_upload() changed the same way.
"""YouTube Uploader and Approval Dispatch Agent for TubePilot AI.

Workflow:
1. Require a passing VerificationReport; refuse on fail
2. Validate the local video (exists, minimum size, ffprobe duration probe)
3. Upload video to YouTube with privacyStatus="private"
4. Verify the YouTube copy exists before touching local files
5. Attach branded thumbnail
6. Record permanent metadata into Google Sheets yt_videos tab
7. Only then execute ephemeral purge: delete local video and thumbnail files
8. Prepare Telegram approval notification payload with 5 action buttons:
   [✅ Publish] [❌ Reject] [🔁 Regenerate] [✏️ Edit] [🖼️ Thumbnail]

Internal linking (26 Nov 2026):
- Every uploaded description gets a "Watch next" block (the
  channel's own related videos, read from the yt_videos Sheet tab —
  never invented links) and a "Sources / References" block built from
  the research bundle's REAL sources (see build_video_description /
  references_from_bundle). The long video's description is uploaded
  first without the Shorts links (their IDs do not exist yet); each
  Short's description links the same day's long video; after all
  Shorts upload, cross_link_daily_uploads() updates the long video's
  description with the 3 Shorts links via videos.update.
- End screens, cards and the Shorts "related video" button are
  YouTube-Studio-only — no API call sets them, and this module never
  claims otherwise. They are explicit steps in the live-upload
  checklist (~/workspace/yt-agent-live-upload-checklist.md).
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from loguru import logger
from pydantic import BaseModel, Field

from db.sheets import get_related_videos, save_short_record, save_video_record
from models.script import ShortsScript, VideoScript
from models.verification import VerificationReport
from storage.ephemeral import purge_local_media
from tools.youtube_api import (
    update_video_description,
    upload_private,
    upload_short,
    verify_video_exists,
)

__all__ = [
    "UploadResult",
    "UploaderAgent",
    "run_upload",
    "run_upload_short",
    "build_video_description",
    "references_from_bundle",
    "cross_link_daily_uploads",
]

# YouTube's description limit (upload_private truncates at the same
# value; the builder enforces it first so blocks are never cut).
MAX_DESCRIPTION_CHARS = 5000
WATCH_NEXT_HEADER = "Watch next:"
SOURCES_HEADER = "Sources / References:"

# A YouTube Short must be 60 seconds or less (the repo's ShortsScript
# model enforces the same limit at the script level; the probe below
# enforces it again at the file level).
MAX_SHORT_DURATION_SEC = 60.0

# Conservative minima: the old placeholder MP4 was ~40 bytes with no
# frames. These are sanity floors, not a quality bar — a real render is
# orders of magnitude larger/longer.
MIN_VIDEO_BYTES = 10_000
MIN_DURATION_SEC = 1.0


# ---------------------------------------------------------------------------
# Internal linking: description templates (26 Nov 2026)
# ---------------------------------------------------------------------------
def references_from_bundle(bundle: Any) -> List[Dict[str, str]]:
    """Build the "Sources / References" entries for a research bundle.

    Returns one entry per REAL source in the bundle — TinyFish
    articles (``claim`` = article title, ``source`` = publisher,
    ``url`` = article URL) — plus one entry naming the trend feeds
    the day's trend step used (``bundle.trend_sources``), if any.
    Entries with no title are dropped; duplicate URLs are deduped.
    Nothing is invented: a topic with no retrieved sources yields an
    empty list (and the description simply has no Sources block).
    Accepts a ResearchBundle or a plain dict with the same keys.
    """
    if bundle is None:
        return []
    if isinstance(bundle, dict):
        raw_sources = bundle.get("sources") or []
        trend_sources = list(bundle.get("trend_sources") or [])
    else:
        raw_sources = list(getattr(bundle, "sources", []) or [])
        trend_sources = list(getattr(bundle, "trend_sources", []) or [])
    out: List[Dict[str, str]] = []
    seen_urls: set = set()
    for src in raw_sources:
        if isinstance(src, dict):
            title = str(src.get("claim") or src.get("title") or "").strip()
            publisher = str(src.get("source") or src.get("publisher") or "").strip()
            url = str(src.get("url") or "").strip()
        else:
            title = str(getattr(src, "claim", "") or "").strip()
            publisher = str(getattr(src, "source", "") or "").strip()
            url = str(getattr(src, "url", "") or "").strip()
        if not title:
            continue
        if url and url in seen_urls:
            continue
        seen_urls.add(url)
        out.append({"title": title, "publisher": publisher, "url": url})
    if trend_sources:
        out.append(
            {
                "title": "Trend signals: " + ", ".join(str(t) for t in trend_sources),
                "publisher": "",
                "url": "",
            }
        )
    return out


def _strip_appended_blocks(description: str) -> str:
    """Remove a previously appended Watch-next / Sources block.

    Makes build_video_description idempotent: rebuilding a
    description that already has the blocks (e.g. the post-Shorts
    update of the long video) never duplicates them.
    """
    for header in (WATCH_NEXT_HEADER, SOURCES_HEADER):
        idx = description.find("\n\n" + header)
        if idx != -1:
            description = description[:idx]
    return description.rstrip()


def build_video_description(
    base_description: str,
    watch_next: Optional[List[Dict[str, Any]]] = None,
    sources: Optional[List[Dict[str, str]]] = None,
) -> str:
    """Append "Watch next" + "Sources / References" to a description.

    Pure function (no API/Sheets calls), so it is unit-testable.

    - ``base_description`` comes first, untouched (it starts with the
      compliance disclaimer — that stays at the top).
    - ``watch_next``: channel videos as ``{"title", "yt_video_id",
      "yt_url"}`` — rendered as ``• Title — https://youtu.be/ID``.
      Entries without an ID/URL are dropped; duplicates (same video
      ID/URL) are deduped, order preserved.
    - ``sources``: ``references_from_bundle()`` entries — rendered as
      ``• Title — Publisher — URL`` (publisher/URL omitted when empty).
    - Total length is capped at YouTube's 5000 characters by shrinking
      the base text (its tail — the disclaimer at the start is never
      cut), never by dropping the link/reference blocks.
    - Re-running on an already-built description replaces the old
      blocks instead of duplicating them.
    """
    base = _strip_appended_blocks(str(base_description or "").strip())
    blocks: List[str] = []

    seen: set = set()
    unique_links: List[Dict[str, Any]] = []
    for entry in watch_next or []:
        key = entry.get("yt_video_id") or entry.get("yt_url") or ""
        if not key or key in seen:
            continue
        seen.add(key)
        unique_links.append(entry)
    if unique_links:
        lines = [WATCH_NEXT_HEADER]
        for entry in unique_links:
            url = entry.get("yt_url") or f"https://youtu.be/{entry.get('yt_video_id')}"
            title = str(entry.get("title") or "Watch next").strip()
            lines.append(f"• {title} — {url}")
        blocks.append("\n".join(lines))

    real_sources = [s for s in (sources or []) if str(s.get("title") or "").strip()]
    if real_sources:
        lines = [SOURCES_HEADER]
        for src in real_sources:
            line = f"• {str(src['title']).strip()}"
            publisher = str(src.get("publisher") or "").strip()
            url = str(src.get("url") or "").strip()
            if publisher:
                line += f" — {publisher}"
            if url:
                line += f" — {url}"
            lines.append(line)
        blocks.append("\n".join(lines))

    if not blocks:
        return base
    joined_blocks = "\n\n".join(blocks)
    if base:
        total = len(base) + 2 + len(joined_blocks)
        if total > MAX_DESCRIPTION_CHARS:
            room = MAX_DESCRIPTION_CHARS - len(joined_blocks) - 2
            base = base[: max(room, 0)].rstrip()
        return base + "\n\n" + joined_blocks
    return joined_blocks


def cross_link_daily_uploads(
    long_yt_video_id: str,
    base_description: str,
    shorts: Optional[List[Any]] = None,
    bundle: Optional[Any] = None,
    related: Optional[List[Dict[str, Any]]] = None,
) -> tuple:
    """Add the day's Shorts links to the long video's description.

    The feasible order (Shorts IDs exist only after upload):
    long video uploads first -> Shorts upload (each linking the long
    video) -> this runs last and updates the long video's description
    via videos.update with a rebuilt "Watch next" block (related
    channel videos + the uploaded Shorts) and the "Sources /
    References" block from the research bundle.

    ``shorts``: uploaded Shorts as dicts (``{"title",
    "yt_video_id"}``, e.g. ``agents/shorts`` results) or
    ``UploadResult`` objects; failed/skipped Shorts (no video ID)
    are ignored. ``related``: pre-fetched related videos; when None
    they are read best-effort from the yt_videos Sheet tab.

    Returns ``(updated, new_description)``. ``updated`` is False when
    the YouTube update failed or was skipped — never raises for an
    API/Sheets failure (the videos exist; only the links are
    missing). Raises ValueError only for a missing/simulated long
    video ID.
    """
    if not long_yt_video_id or str(long_yt_video_id).startswith(("yt_sim_", "yt_priv_")):
        raise ValueError(
            f"Refusing to cross-link simulated video ID '{long_yt_video_id}'."
        )
    try:
        uploaded_shorts: List[Dict[str, Any]] = []
        for short in shorts or []:
            if isinstance(short, dict):
                vid = short.get("yt_video_id")
                title = short.get("title")
            else:
                vid = getattr(short, "yt_video_id", None)
                title = getattr(short, "title", None)
            if vid:
                uploaded_shorts.append(
                    {
                        "title": str(title or "Short").strip(),
                        "yt_video_id": vid,
                        "yt_url": f"https://youtu.be/{vid}",
                    }
                )
        watch_next: List[Dict[str, Any]] = list(related) if related is not None else []
        if related is None:
            try:
                topic = getattr(bundle, "topic", "") if bundle is not None else ""
                pillar = getattr(bundle, "pillar", None) if bundle is not None else None
                watch_next = get_related_videos(
                    topic=str(topic or ""),
                    pillar=pillar,
                    limit=5,
                    exclude_yt_video_ids=[str(long_yt_video_id)],
                )
            except Exception as exc:  # noqa: BLE001 — Sheets outage: link without related
                logger.warning(
                    f"Related-videos lookup failed ({exc}); cross-linking "
                    "the long video without related videos."
                )
                watch_next = []
        watch_next = watch_next + uploaded_shorts
        new_description = build_video_description(
            base_description,
            watch_next=watch_next,
            sources=references_from_bundle(bundle),
        )
        updated = update_video_description(long_yt_video_id, new_description)
        if updated:
            logger.info(
                f"Long video {long_yt_video_id} description updated with "
                f"{len(uploaded_shorts)} Short link(s) + references."
            )
        else:
            logger.warning(
                f"Long video {long_yt_video_id} description NOT updated "
                "(YouTube update failed); Shorts links are missing from "
                "its description — retry cross_link_daily_uploads manually."
            )
        return updated, new_description
    except Exception as exc:  # noqa: BLE001 — cross-linking must never fail the day
        logger.warning(
            f"Cross-linking failed for {long_yt_video_id} ({exc}); "
            "descriptions left as uploaded."
        )
        try:
            return False, build_video_description(
                base_description, sources=references_from_bundle(bundle)
            )
        except Exception:
            return False, str(base_description or "")


class UploadResult(BaseModel):
    """Result of YouTube private upload and Google Sheets registration."""
    video_id: str
    yt_video_id: str
    yt_url: str
    status: str = "pending_approval"
    title: str
    purged_local_bytes: int = 0
    approval_buttons: list[list[str]] = Field(default_factory=list)
    # "long" for the daily long-form video, "short" for a Short.
    format: str = "long"
    # False only when the Sheets record could not be saved (Shorts
    # only — the long-form path still raises on Sheets failure). The
    # YouTube upload itself succeeded in that case; the video ID is
    # in this result and in the run log for manual recovery.
    sheets_saved: bool = True


def _probe_duration_sec(video_path: Path) -> float:
    """Return the real duration of *video_path* via ffprobe.

    Raises if ffprobe is unavailable, the probe fails, or no duration
    is reported. A video that cannot be probed is never uploaded.
    """
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "json",
                str(video_path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except FileNotFoundError as e:
        raise RuntimeError(
            "ffprobe is not available, so the video cannot be probed "
            "before upload. Install ffmpeg/ffprobe; unverified files "
            "are never uploaded."
        ) from e
    if result.returncode != 0:
        raise RuntimeError(
            f"ffprobe failed for {video_path}: {result.stderr.strip() or result.stdout.strip()}"
        )
    try:
        duration = float(json.loads(result.stdout)["format"]["duration"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as e:
        raise RuntimeError(f"ffprobe reported no usable duration for {video_path}: {e}") from e
    if duration <= 0:
        raise RuntimeError(f"ffprobe reported a non-positive duration ({duration}s) for {video_path}.")
    return duration


class UploaderAgent:
    """Manages secure private video publishing, Google Sheets persistence, and local disk wiping."""

    def __init__(self):
        pass

    def upload_and_register(
        self,
        video_path: str,
        thumb_path: Optional[str],
        script: VideoScript,
        verification_report: VerificationReport,
        cost_usd: float = 0.0,
        bundle: Optional[Any] = None,
        watch_next: Optional[List[Dict[str, Any]]] = None,
    ) -> UploadResult:
        """Upload video privately, record in Google Sheets, purge local files, and return approval payload.

        Args:
            verification_report: REQUIRED — the real VerificationReport
                from agents.verify.run_verification() for this script.
                Upload is refused unless report.overall == "pass" and a
                real numeric report.confidence is present. The recorded
                verification_score is report.confidence, never a
                hardcoded value. Callers (orchestration/graph.py
                node_finalize) must pass the real report; do not call
                this with a fabricated report or with None.
            bundle: optional ResearchBundle — when given, the uploaded
                description gets a "Sources / References" block from
                its real sources, and a "Watch next" block of related
                channel videos (read best-effort from the yt_videos
                Sheet tab, unless ``watch_next`` is supplied). A
                Sheets failure leaves the plain SEO description rather
                than failing the upload.
            watch_next: optional explicit "Watch next" entries
                (``{"title", "yt_video_id", "yt_url"}``); overrides the
                Sheet lookup when given (even an empty list means "no
                Watch next block").

        Local files are purged ONLY after the YouTube copy has been
        verified (videos.list) AND the Google Sheets record is saved. Any
        failure before that point raises and leaves local files intact.
        """
        # 0. Verification gate — fail verdicts never reach YouTube.
        if verification_report is None:
            raise ValueError(
                "upload_and_register() requires a VerificationReport; "
                "got None. Pass the real report from run_verification()."
            )
        overall = getattr(verification_report, "overall", None)
        confidence = getattr(verification_report, "confidence", None)
        if overall != "pass":
            raise ValueError(
                f"Refusing upload: verification verdict is '{overall}', "
                "not 'pass'. Fix the reported issues and re-verify; "
                "a failed script must never be uploaded."
            )
        if confidence is None or not isinstance(confidence, (int, float)):
            raise ValueError(
                "Refusing upload: VerificationReport has no real numeric "
                f"confidence score (got {confidence!r}). The recorded "
                "verification_score must come from the real report."
            )
        verification_score = float(confidence)

        # 0b. Local video validation — existence, size, probe.
        v_path = Path(video_path)
        if not v_path.exists():
            raise FileNotFoundError(f"Video to upload not found: {video_path}")

        file_size = v_path.stat().st_size
        if file_size < MIN_VIDEO_BYTES:
            raise ValueError(
                f"Refusing upload: video is only {file_size} bytes "
                f"(minimum {MIN_VIDEO_BYTES}); this looks like an empty "
                "placeholder render, not a real video."
            )
        duration_sec = _probe_duration_sec(v_path)
        if duration_sec < MIN_DURATION_SEC:
            raise ValueError(
                f"Refusing upload: probed duration is {duration_sec}s "
                f"(minimum {MIN_DURATION_SEC}s)."
            )
        logger.info(f"Uploading private video to YouTube ({file_size} bytes, {duration_sec}s): '{script.seo.title}'")

        # 0c. Thumbnail validation — empty is warned/rejected, never "".
        thumbnail_url: Optional[str] = None
        if thumb_path:
            t_path = Path(thumb_path)
            if not t_path.exists():
                raise FileNotFoundError(f"Thumbnail supplied but not found: {thumb_path}")
            if t_path.stat().st_size == 0:
                raise ValueError(f"Refusing upload: thumbnail is a 0-byte file: {thumb_path}")
            thumbnail_url = str(t_path.resolve())
        else:
            logger.warning(
                "No thumbnail supplied for upload; proceeding without one. "
                "thumbnail_url will be recorded as null, not as an empty string."
            )

        # 1. YouTube API private upload (raises on any failure; no fake ID).
        # Internal linking: the uploaded description carries a "Watch
        # next" block (related channel videos) + a "Sources /
        # References" block from the bundle's real sources. The day's
        # Shorts cannot be linked yet (no IDs until they upload) —
        # cross_link_daily_uploads() adds them afterwards.
        linked_description = script.seo.description
        try:
            related = list(watch_next) if watch_next is not None else None
            if related is None and bundle is not None:
                related = get_related_videos(
                    topic=script.seo.title, pillar=script.pillar, limit=5
                )
            linked_description = build_video_description(
                script.seo.description,
                watch_next=related,
                sources=references_from_bundle(bundle),
            )
        except Exception as exc:  # noqa: BLE001 — linking must never fail an upload
            logger.warning(
                f"Internal-linking description build failed ({exc}); "
                "uploading with the plain SEO description."
            )
        seo_dict = script.seo.model_dump()
        seo_dict["description"] = linked_description
        yt_video_id = upload_private(str(v_path.resolve()), thumb_path, seo_dict)
        yt_url = f"https://youtu.be/{yt_video_id}"

        # 1b. Verify the YouTube copy exists BEFORE any purge or DB write
        # that would treat the upload as done. Raises on failure.
        if not verify_video_exists(yt_video_id):
            raise RuntimeError(
                f"YouTube copy of uploaded video {yt_video_id} could not "
                "be verified via videos.list; local files are NOT purged "
                "and no record is saved."
            )
        logger.info(f"YouTube copy verified for video {yt_video_id}.")

        # 2. Save metadata to Google Sheets yt_videos
        video_record = {
            "pillar": script.pillar,
            "topic": script.seo.title,
            "yt_video_id": yt_video_id,
            "yt_url": yt_url,
            "title": script.seo.title,
            "description_preview": script.seo.description[:200],
            "description": linked_description,
            "tags": script.seo.tags,
            "thumbnail_url": thumbnail_url,
            "script_json": script.model_dump_json(),
            "verification_score": verification_score,
            "verification_overall": overall,
            "duration_sec": duration_sec,
            "cost_usd": cost_usd,
            "status": "pending_approval",
            "created_at": int(time.time()),
        }
        db_video_id = save_video_record(video_record)

        # 3. Purge local video and thumbnail files ONLY now that the
        # YouTube copy is verified and the DB record is saved. If purge
        # itself raises, the error propagates and the result is not
        # returned as a successful upload.
        purge_local_media(video_path, thumb_path)
        logger.info(f"Ephemeral purge completed. Local video {v_path.name} deleted.")

        # 4. Telegram approval buttons payload
        buttons = [
            ["✅ Publish", f"pub_{yt_video_id}"],
            ["❌ Reject", f"rej_{yt_video_id}"],
            ["🔁 Regenerate", f"reg_{db_video_id}"],
            ["✏️ Edit", f"edt_{db_video_id}"],
            ["🖼️ Thumbnail", f"thm_{yt_video_id}"],
        ]

        return UploadResult(
            video_id=db_video_id,
            yt_video_id=yt_video_id,
            yt_url=yt_url,
            status="pending_approval",
            title=script.seo.title,
            purged_local_bytes=file_size,
            approval_buttons=buttons,
        )


    def upload_short_and_register(
        self,
        video_path: str,
        thumb_path: Optional[str],
        short_script: ShortsScript,
        parent_topic: str,
        short_index: int = 0,
        cost_usd: float = 0.0,
        bundle: Optional[Any] = None,
        long_video: Optional[Any] = None,
    ) -> UploadResult:
        """Upload one vertical Short privately and register it separately.

        ``bundle``: optional ResearchBundle — when given, the Short's
        description gets a "Sources / References" block from its real
        sources (same as the long video). ``long_video``: the same
        day's long video as ``{"title", "yt_video_id", "yt_url"}`` (or
        a bare YouTube video ID string) — when given, the Short's
        description gets a "Watch next" block linking the long video,
        so each Short points at the full breakdown. A linking failure
        never fails the upload (plain SEO description is used).

        Separate path from :meth:`upload_and_register`:

        - No long-form VerificationReport is used — the verify agent
          only audits the long-form VideoScript, so a Short cannot
          borrow its verdict. The Short's own gate is: (a) the
          ShortsScript model itself (pydantic enforces <= 3 scenes,
          total <= 60s, 9:16, no forbidden investment-advice phrases
          at construction), (b) a disclaimer-led SEO description,
          (c) the rendered file passing the size probe and an ffprobe
          duration probe of <= 60s.
        - Upload goes through :func:`tools.youtube_api.upload_short`
          (#Shorts in title/description), the YouTube copy is verified
          via videos.list, and the record is saved to the separate
          ``yt_shorts`` tab (never yt_videos), with ``format="short"``,
          the day's ``parent_topic`` and this Short's index — so a
          Short's failure/status never touches the long video's record.
        - Sheets failure is NON-FATAL here (unlike the long-form
          path): the YouTube upload already succeeded and verified,
          so the result is still returned with ``sheets_saved=False``
          and the local file is NOT purged, leaving it on disk for a
          manual re-register. Raising would lose a good upload.
        """
        # (a) ShortsScript model validation already ran at construction;
        # re-check the two gates that depend on caller-supplied values.
        if not short_script.seo.description.startswith("DISCLAIMER"):
            raise ValueError(
                "Refusing Short upload: Short SEO description does not "
                "start with the compliance disclaimer."
            )
        if short_script.aspect_ratio != "9:16":
            raise ValueError(
                f"Refusing Short upload: aspect_ratio is "
                f"{short_script.aspect_ratio!r}, not '9:16'."
            )

        # (b) Local video validation — existence, size, duration <= 60s.
        v_path = Path(video_path)
        if not v_path.exists():
            raise FileNotFoundError(f"Short video to upload not found: {video_path}")
        file_size = v_path.stat().st_size
        if file_size < MIN_VIDEO_BYTES:
            raise ValueError(
                f"Refusing Short upload: video is only {file_size} bytes "
                f"(minimum {MIN_VIDEO_BYTES})."
            )
        duration_sec = _probe_duration_sec(v_path)
        if duration_sec < MIN_DURATION_SEC:
            raise ValueError(
                f"Refusing Short upload: probed duration is {duration_sec}s "
                f"(minimum {MIN_DURATION_SEC}s)."
            )
        if duration_sec > MAX_SHORT_DURATION_SEC:
            raise ValueError(
                f"Refusing Short upload: probed duration is {duration_sec}s "
                f"(maximum {MAX_SHORT_DURATION_SEC}s for a Short)."
            )
        logger.info(
            f"Uploading private Short ({file_size} bytes, {duration_sec}s): "
            f"'{short_script.seo.title}'"
        )

        # (c) Thumbnail validation — same rule as the long-form path.
        thumbnail_url: Optional[str] = None
        if thumb_path:
            t_path = Path(thumb_path)
            if not t_path.exists():
                raise FileNotFoundError(f"Thumbnail supplied but not found: {thumb_path}")
            if t_path.stat().st_size == 0:
                raise ValueError(f"Refusing Short upload: thumbnail is a 0-byte file: {thumb_path}")
            thumbnail_url = str(t_path.resolve())
        else:
            logger.warning("No thumbnail supplied for Short upload; proceeding without one.")

        # 1. YouTube API private Short upload (#Shorts SEO).
        # Internal linking: the Short's description links the same
        # day's long video (when known) and cites the bundle's real
        # sources. shorts_seo() prepends "#Shorts" afterwards.
        linked_description = short_script.seo.description
        try:
            watch_next: List[Dict[str, Any]] = []
            if long_video is not None:
                if isinstance(long_video, str):
                    watch_next.append(
                        {
                            "title": "Full breakdown — watch the long video",
                            "yt_video_id": long_video,
                            "yt_url": f"https://youtu.be/{long_video}",
                        }
                    )
                else:
                    vid = long_video.get("yt_video_id")
                    if vid:
                        watch_next.append(
                            {
                                "title": str(
                                    long_video.get("title")
                                    or "Full breakdown — watch the long video"
                                ).strip(),
                                "yt_video_id": vid,
                                "yt_url": long_video.get("yt_url")
                                or f"https://youtu.be/{vid}",
                            }
                        )
            linked_description = build_video_description(
                short_script.seo.description,
                watch_next=watch_next,
                sources=references_from_bundle(bundle),
            )
        except Exception as exc:  # noqa: BLE001 — linking must never fail an upload
            logger.warning(
                f"Internal-linking description build failed for Short "
                f"({exc}); uploading with the plain SEO description."
            )
        seo_dict = short_script.seo.model_dump()
        seo_dict["description"] = linked_description
        yt_video_id = upload_short(str(v_path.resolve()), thumb_path, seo_dict)
        yt_url = f"https://youtu.be/{yt_video_id}"

        # 1b. Verify the YouTube copy exists BEFORE any purge or DB write.
        if not verify_video_exists(yt_video_id):
            raise RuntimeError(
                f"YouTube copy of uploaded Short {yt_video_id} could not "
                "be verified via videos.list; local files are NOT purged "
                "and no record is saved."
            )
        logger.info(f"YouTube copy verified for Short {yt_video_id}.")

        # 2. Save metadata to the separate Google Sheets yt_shorts tab.
        # Non-fatal: a Sheets outage must not lose a verified upload.
        sheets_saved = True
        short_record = {
            "parent_topic": parent_topic,
            "topic": short_script.seo.title,
            "yt_video_id": yt_video_id,
            "yt_url": yt_url,
            "title": short_script.seo.title,
            "description_preview": short_script.seo.description[:200],
            "description": linked_description,
            "tags": short_script.seo.tags,
            "thumbnail_url": thumbnail_url,
            "script_json": short_script.model_dump_json(),
            "duration_sec": duration_sec,
            "cost_usd": cost_usd,
            "short_index": short_index,
            "format": "short",
            "status": "pending_approval",
            "created_at": int(time.time()),
        }
        try:
            db_short_id = save_short_record(short_record)
        except Exception as exc:  # noqa: BLE001 — non-fatal by design
            sheets_saved = False
            db_short_id = f"short_{yt_video_id}"
            logger.error(
                f"Could not save Short record to Google Sheets yt_shorts "
                f"({exc}); YouTube upload {yt_video_id} stands, local "
                f"file kept for manual re-register."
            )

        # 3. Purge local files ONLY when the YouTube copy is verified AND
        # the Sheet record is saved; otherwise keep the file on disk.
        purged_bytes = 0
        if sheets_saved:
            purge_local_media(video_path, thumb_path)
            purged_bytes = file_size
            logger.info(f"Ephemeral purge completed. Local Short {v_path.name} deleted.")

        buttons = [
            ["✅ Publish", f"pub_{yt_video_id}"],
            ["❌ Reject", f"rej_{yt_video_id}"],
        ]

        return UploadResult(
            video_id=db_short_id,
            yt_video_id=yt_video_id,
            yt_url=yt_url,
            status="pending_approval",
            title=short_script.seo.title,
            purged_local_bytes=purged_bytes,
            approval_buttons=buttons,
            format="short",
            sheets_saved=sheets_saved,
        )


def run_upload(
    video_path: str,
    thumb_path: Optional[str],
    script: VideoScript,
    verification_report: VerificationReport,
    cost_usd: float = 0.0,
    bundle: Optional[Any] = None,
    watch_next: Optional[List[Dict[str, Any]]] = None,
) -> UploadResult:
    """Convenience functional interface for video uploading and registration.

    verification_report is required (see UploaderAgent.upload_and_register):
    pass the real VerificationReport from run_verification().
    ``bundle`` / ``watch_next`` enable the internal-linking description
    blocks (see upload_and_register).
    """
    return UploaderAgent().upload_and_register(
        video_path, thumb_path, script, verification_report, cost_usd,
        bundle=bundle, watch_next=watch_next,
    )


def run_upload_short(
    video_path: str,
    thumb_path: Optional[str],
    short_script: ShortsScript,
    parent_topic: str,
    short_index: int = 0,
    cost_usd: float = 0.0,
    bundle: Optional[Any] = None,
    long_video: Optional[Any] = None,
) -> UploadResult:
    """Convenience functional interface for Shorts uploading/registration.

    Separate from :func:`run_upload`: no long-form VerificationReport
    is required or accepted — Shorts are gated on their own
    ShortsScript validation (see UploaderAgent.upload_short_and_register).
    ``bundle`` adds the Sources block; ``long_video`` (``{"title",
    "yt_video_id", ...}`` or a bare video ID) adds the "Watch next"
    link to the same day's long video.
    """
    return UploaderAgent().upload_short_and_register(
        video_path, thumb_path, short_script, parent_topic, short_index, cost_usd,
        bundle=bundle, long_video=long_video,
    )
