# TubePilot AI — Progress Tracker

## Phase 1: Foundation & Data Models
- [x] Pydantic models (`models/research.py`, `models/script.py`, `models/verification.py`, `models/__init__.py`)
- [x] Config manager (`config_manager.py` with DynamoDB + local fallback + 5s cache)
- [x] Secrets handler (`tools/secrets.py` with Fernet PBKDF2 encryption + DynamoDB + env fallback)
- [x] Project scaffold (`requirements.txt`, `.gitignore`, `.env.example`, all package `__init__.py`)

## Phase 2: External Tools
- [x] `tools/llm_provider.py` (Bedrock + Gemini + OpenRouter with retry & fallback chain)
- [x] `tools/image_gen.py` (Imagen 3 + Gemini Flash + FLUX, cached by MD5, dark mode styling)
- [x] `tools/sec_edgar.py` (SEC 10-K / 10-Q filing extractor, fair access user-agent)
- [x] `tools/yfinance_tool.py` (Financial metrics, revenue segments, margins, competitors)
- [x] `tools/tinyfish.py` (Fintech news & context crawler)
- [x] `tools/youtube_api.py` (Competitor landscape, search, upload, publish, delete)
- [x] `tools/pixabay.py` (Stock footage & background music search)
- [x] `tools/fal_ai.py` (LTX-Video AI generation)
- [x] `tools/replicate_tool.py` (FLUX schnell fallback)
- [x] `tools/polly.py` (AWS Polly Neural / Standard TTS, SSML prosody, duration detection)
- [x] `tests/test_tools.py` (9/9 unit tests passing)

## Phase 3: AI Agents (Research, Script, Verification)
- [x] `agents/research.py` & `prompts/research_agent.txt` (Multi-source research with deterministic provenance)
- [x] `agents/script.py` & `prompts/script_*.txt` (Longform 8+ charts + 3 daily Shorts)
- [x] `agents/verify.py` & `prompts/verify_*.txt` (7 deterministic <1s checks + 2 LLM semantic checks)
- [x] `tests/test_research.py` (2/2 tests passing)
- [x] `tests/test_verify.py` (7/7 mandatory tests passing with sub-second deterministic checks: 0-14ms)

## Phase 4: LangGraph Orchestration & Self-Healing
- [x] `orchestration/graph.py` (State machine: research -> script -> verify -> [fix loop max 3] -> finalize / human_review)
- [x] `pipeline.py` (`run_stage` wrapper with autonomous error classification, backoff, and healing)
- [x] `workflow/errors.py` (7 error classes taxonomy)
- [x] `workflow/healer.py` (Autonomous healing actions: retry_same, switch_provider, switch_key, reschedule_next_day, re_prompt, abort)
- [x] `tests/test_healer.py` (6/6 mandatory tests passing in 0.003s)
- [x] `tests/test_graph.py` (4/4 tests passing: happy path, fix loop, human review escalation, self-healing retry)

## Phase 5: Media Pipeline (Charts + TTS + Visuals + Assembly)
- [x] `agents/charts.py` (8 branded institutional chart types: revenue_trend, segment_breakdown, margin_compare, unit_economics_waterfall, scope_tam, competitor_matrix, growth_margin_scatter, market_share_donut)
- [x] `agents/tts.py` (Polly Neural synthesis, duration alignment back into scene model)
- [x] `agents/visuals.py` (Budget-aware visual asset routing, $0.30/video cap enforcement)
- [x] `agents/assemble.py` (MoviePy + FFmpeg compositor, subtitle burn-in, Longform 16:9 + Shorts 9:16)
- [x] `tests/test_media.py` (6/6 tests passing, sample chart verified)

## Phase 6: Upload & Ephemeral Storage
- [x] `agents/uploader.py` (Private upload, thumbnail attachment, DynamoDB registration, immediate local disk purge, 5 Telegram action buttons)
- [x] `db/setup_dynamo.py` (Provisioned 5 RCU/WCU setup script for all 7 tables with GSI and TTL rules)
- [x] `db/dynamo.py` (Complete CRUD layer for yt_videos, yt_research_cache, yt_verification_log, yt_pipeline_runs, yt_topics_queue, yt_bot_requests)
- [x] `storage/ephemeral.py` (4 cleanup layers: atexit, S3 lifecycle, DynamoDB TTL, startup defensive sweep)
- [x] `tests/test_storage_uploader.py` (7/7 tests passing)

## Phase 7: Telegram Bot & Control Plane
- [x] `telegram_bot/bot.py` & command handlers (/ping, /run, /status, /pause, /resume, /keys, /model, /topic, /config, /codespace, and 5 interactive callbacks)
- [x] `telegram_bot/keys.py` (Hot-swapping encrypted keys, masking security, auto-delete instruction)
- [x] `telegram_bot/health.py` (Sub-5s health check, latency measurement, API diagnostics, daily budget spend)
- [x] `telegram_bot/codespace.py` (GitHub Codespaces inspection and immediate kill switch)
- [x] `telegram_bot/dispatch.py` (GitHub Actions workflow dispatch trigger + local topic queueing)
- [x] `tests/test_bot.py` (8/8 unit tests passing)

## Phase 8: CI/CD, Docs & Final Integration
- [x] `scripts/notify.py` (Telegram markdown alerts + inline action buttons)
- [x] `scripts/notify_failure.py` (Automated failure alert dispatch with GitHub run context)
- [x] `scripts/notify_success.py` (Automated success dispatch with private URL and 5 action buttons)
- [x] `scripts/dev_run.py` (Local/Codespaces runner with guaranteed `finally` Codespace shutdown)
- [x] `.github/workflows/daily.yml` (Cron 0 4 * * *, manual dispatch, retry logic, error handling)
- [x] `.devcontainer/devcontainer.json` (Python 3.11, AWS CLI, gh CLI, auto-stop on 10 min idle)
- [x] `README.md` (Full architecture, setup guide, Telegram cheatsheet, error taxonomy)
- [x] `tests/test_integration.py` (5/5 tests passing: notify, success/failure formatting, dev_run lifecycle, codespace stop)

## Last Verified Commands
- `python -m unittest tests/test_bot.py -v` -> `Ran 8 tests in 3.899s. OK` (Verified 2026-11-22)
- `python -m unittest tests/test_integration.py -v` -> `Ran 5 tests in 1.431s. OK` (Verified 2026-11-22)
- `python -m unittest discover -s tests -p "test_*.py" -v` -> `Ran 54 tests in 24.120s. OK` (All 54 tests across all 8 Phases verified passing 2026-11-22)


## Open Issues
(none)

## Deferred
(none)
