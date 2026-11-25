# TubePilot AI — Design Decisions & Assumptions

## D-001: DynamoDB Provisioned Mode
Using PROVISIONED with 5 RCU/5 WCU per table to avoid pay-per-request surprises.

> Superseded by D-011 (25 Nov 2026): DynamoDB removed; data now in Google Sheets.

## D-002: Config Snapshot at Pipeline Start
Config is frozen at pipeline start to prevent mid-run race conditions from bot changes.

## D-003: Deterministic Verification First
7 deterministic checks run before any LLM call. Saves ~90% LLM costs.

## D-004: YouTube as Primary Storage
Zero persistent video storage. YouTube IS the storage. Local files deleted after confirmed upload.

## D-005: Free Tier Priority
All services use free tier except optional Gemini (~$0.30/month for verification).

## D-006: Shorts Pipeline
Shorts reuse the same research bundle as the long-form video. 3 shorts per daily run, each 60 sec max, 9:16 aspect ratio.

## D-007: Telegram Hot-Swapping & In-Memory Fallback
Bot handles key hot-swapping through encrypted Fernet PBKDF2 without application restarts. All `/keys set` commands request 30-second user deletion. When DynamoDB is offline (local development/unit tests), an in-memory fallback cache securely stores values for the lifetime of the process.

> Superseded by D-011 (25 Nov 2026): no Fernet store, no in-memory fallback; secrets live in GitHub repo secrets, `/keys set`/`delete`/`rotate` direct the operator there.

## D-008: Automated Codespace Shutdown
To prevent idle compute billing when running inside GitHub Codespaces, `scripts/dev_run.py` explicitly invokes `gh codespace stop` within a `finally` block unless `--no-stop` is passed. `.devcontainer/devcontainer.json` also configures `shutdownAction: stopContainer`.

## D-009: Gemini Multi-Key Failover Pool
To maximize uptime on the Google AI Studio free tier (15 RPM / 1,500 RPD) without unexpected failure or cost, TubePilot AI implements a multi-key pool for Gemini. Keys can be registered under `GEMINI_KEY`, `GEMINI_KEY_POOL`, or indexed `GEMINI_KEY_1`..`GEMINI_KEY_20`. On HTTP 429 (rate-limit) or 401/403 (quota/revocation), the failing key enters a timed cooldown (60s for 429, 3600s for 401/403) and the system automatically fails over to the next healthy standby key in the pool across both LLM prompts (`tools/llm_provider.py`) and Imagen 3 asset generation (`tools/image_gen.py`). Telegram control plane supports `/keys pool list|add|rotate`.

## D-010: Channel Branding & CTA Options with Strict Verification Gating
1. **Dynamic Channel & CTA Customization:** Channel name (default: "TubePilot AI") and call-to-actions (like & subscribe, midroll, outro, comment prompts) are persisted in `config_manager.py` (Google Sheets `yt_config` tab + in-memory cache). Script generation (`agents/script.py`) injects the configured channel name and CTAs into Scene 13 (outro), SEO metadata (YouTube description/tags), and each of the 3 daily Shorts. Users can view and update these directly via Telegram (`/channel [new_name]`, `/cta [new_text]`, or `/config set channel_name <name>`).
2. **Strict Fact-Check-Before-Video Gate:** The pipeline sequence strictly enforces: **Research -> Script Generation -> Fact Check Audit (Verification) -> Media Assembly**. The video generation and rendering stages (`agents/charts.py`, `agents/tts.py`, `agents/visuals.py`, `agents/assemble.py`) and upload stage (`agents/uploader.py`) are strictly gated by `VerificationReport.outcome == "PASS"`. If verification identifies critical errors or unfixable claims, the pipeline aborts or escalates to Telegram human review; zero media assets are assembled, preventing wasted compute, visual API spend, and video generation on unverified or hallucinated facts.

## D-011: Google Sheets Replaces DynamoDB (25 Nov 2026)

DynamoDB is removed entirely. All non-secret data (videos, research
cache, verification logs, pipeline runs, config, bot requests, topics
queue, YouTube insights) lives in one Google Spreadsheet with one tab
per data set (`db/sheets.py`, 8 tabs), accessed with a service account
(`GOOGLE_SHEETS_ID` + `GOOGLE_SERVICE_ACCOUNT_JSON`). Secrets stay in
GitHub repo secrets (env vars) — none is ever stored in Sheets.
`tools/secrets.py` reads env only and raises on a missing secret;
`/keys set`/`delete`/`rotate`/`export`/`import` now direct the operator
to GitHub repo settings. Tests mock the Sheets client
(`tests/_sheets_fake.py`); the old `ALLOW_IN_MEMORY_*` flags are gone.
Live DynamoDB data could not be migrated: AWS credentials exist
nowhere (not in repo secrets, not in the Codespace, no `~/.aws`), so
live DynamoDB is unreachable and no backups exist — the Sheets store
starts fresh. Polly/Bedrock boto3 clients are unaffected (not DynamoDB).
