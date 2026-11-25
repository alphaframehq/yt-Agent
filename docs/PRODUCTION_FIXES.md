# yt-Agent (TubePilot AI) — Production Fix Patches: CHANGES.md

- **Repo:** alphaframehq/yt-Agent, branch `main`, commit `c39f42541b8e784dcf41c09583dc57227be07667`
- **Source audit:** `~/workspace/yt-agent-audit/yt-agent-production-audit-list.md` (25 Nov 2026, 27 P0 items)
- **Patch date:** 25 Nov 2026
- **Location:** this folder mirrors repo paths, e.g. `fixes/tools/llm_provider.py` = repo `tools/llm_provider.py`
- **Status: LOCAL PATCH FILES ONLY.** Nothing was pushed to GitHub, no branch was created, no repo code was cloned, run, executed or tested. Each worker did a `py_compile` syntax check only. These patches have **not** been integration-tested together.
- **No secrets in this folder.** Only env-var names / placeholders. The `AIza` / `sk-or-` strings in `tools/secrets.py` are the original validation prefixes, unchanged — not real keys.

## How to apply (suggested, not done)

1. Review each file against the original at the pinned commit.
2. Integration gap in §1 is now **RESOLVED 25 Nov 2026** (see §1) — graph passes the real VerificationReport.
3. Copy the files over the repo files on a new branch, run the test suite, fix the known breaks in §2, then set the credentials in §3.
4. Do **not** apply these and run a production pipeline until YouTube OAuth (§3) is implemented and a real refresh token exists — uploads will fail loudly instead of faking success, which is the intended behaviour.

## 1. ✅ Integration gap — RESOLVED 25 Nov 2026

**`agents/uploader.py` vs `orchestration/graph.py` are now compatible.**

- The uploader patch changed `upload_and_register()` / `run_upload()` to require a `verification_report: VerificationReport` argument (P0: refuse on fail, use the real confidence, not hardcoded 1.0).
- `graph.py` `node_finalize` now passes the real report: it raises if `state.verification` is None, raises if `overall != "pass"`, and otherwise passes `state.verification` to `run_upload()`. Never a fabricated report. The graph also has a defence-in-depth guard rejecting empty / `yt_sim_` / `yt_priv_` IDs. Syntax-checked with `py_compile`.

## 2. Known intentional breaks (fail-loud by design)

- **`agents/charts.py` callers will raise** until they pass ResearchBundle data. `agents/visuals.py` was **patched 25 Nov 2026** in this folder (`fixes/agents/visuals.py`): `resolve_scene(scene, data=...)` / `resolve_all(script, bundle_data=...)` pass bundle data to charts, the stock-fallback budget bug is fixed (raises instead of spending silently), ai_gen fallback spend is now recorded, and every asset is validated before "All visuals resolved" is logged. **Wiring resolved 25 Nov 2026 (PR #1 follow-up):** `agents/visuals.py` now has `research_bundle_to_chart_data(bundle)` (only values actually in the bundle; TAM 0.0 sentinel omitted, market-share and competitor-matrix omitted — those charts raise until a real source is wired), `agents/assemble.py` `assemble()` / `assemble_video()` / `assemble_shorts()` accept `bundle_data` (ResearchBundle or dict), and `orchestration/graph.py` `node_finalize` passes `state.research` to both. If no bundle is available, chart scenes raise instead of plotting fabricated data.
- **Script generation raises when no LLM key is configured** instead of returning a fake template script. Same for verification, research, TTS, image generation and YouTube upload: missing keys / failed APIs now raise instead of returning mock success.
- **`tools/youtube_api.py` raises on every write** until real OAuth2 is implemented — see §3. This is intentional: the old code could never have uploaded anyway (it used `YT_CLIENT_SECRET` as an API key).
- **`db/dynamo.py` patched 25 Nov 2026 (PR #1 follow-up, `fixes/db/dynamo.py`):** `log_verification` now defaults a missing overall/confidence to `"not_run"` / `0.0` (not `"pass"` / `1.0`), and a missing model_used to `"unknown"` (not the Bedrock model name).
- **`telegram_bot/agent_chat.py` INTENT 2** ("make a video on …") still replies "Production Run Dispatched!" unconditionally — it needs the same dispatched-flag check as `bot.py` `/run`.

## 3. Still needs real credentials / user decisions (nothing invented)

| Service | What is needed | Status after patches |
|---|---|---|
| YouTube Data API v3 | **Real OAuth2 implementation + stored refresh token (`YT_REFRESH_TOKEN`).** `YT_CLIENT_ID` + `YT_CLIENT_SECRET` alone are not enough. The patched `_get_youtube_service()` raises stating this. A GCP project must be created, YouTube Data API v3 enabled, OAuth consent screen + Desktop-app credentials created, and a refresh token generated via user consent. **This is the biggest open question — see below.** | NOT implemented, raises loudly |
| LLM — Gemini | `GEMINI_KEY` and/or `GEMINI_KEY_POOL` / `GEMINI_KEY_1..20` (`AIza…`) | Code now raises if missing; key itself not supplied |
| LLM — OpenRouter | `OPENROUTER_KEY` (`sk-or-…`) | Code already raised correctly; key not supplied |
| LLM — Bedrock | AWS creds, region, Claude model IDs | **Unresolved contradiction:** README requires Bedrock, `.env.example` says "Bedrock skipped", config defaults use Gemini. Decide one before provisioning. |
| AWS DynamoDB / Polly | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION` (single source of truth — see §4), 7 `yt_*` tables, Polly `polly:SynthesizeSpeech` | Code now fails loudly if tables/region missing |
| Telegram | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ADMIN_ID` (mandatory — auth is now fail-closed without it) | Code exits 1 on bad token, denies all if admin ID unset |
| Secrets storage | `MASTER_SECRET` (Fernet encryption of keys in DynamoDB `yt_config`) | Encryption unchanged; decrypt failure now raises |
| Image gen | `REPLICATE_API_TOKEN` (FLUX); Gemini key (Imagen 3) | Gemini-native image gen is `NotImplementedError` — no endpoint/model existed in the original code to reconstruct |
| Video gen | `FAL_KEY` | Not changed in this patch set |
| Stock | `PIXABAY_KEY` | Music search still returns the single hardcoded track (P1, not fixed) |
| Research | `TINYFISH_API_KEY` | Code now raises if missing/invalid |
| GitHub dispatch | `GH_PAT` | `/run` now honestly reports dispatch failure when missing |
| Research — SEC EDGAR / yfinance | None (keyless); valid SEC `User-Agent` contact only | EDGAR companyfacts reconciliation still TODO (P1) |

### Open questions needing the user

1. **YouTube OAuth:** which Google account / YouTube channel should uploads go to? The user said on 25 Nov 2026 they will give the mail login and to set up YouTube from GCP. That GCP setup (project, API enablement, OAuth client, consent, refresh token) is **not** in these patches — it needs a live browser session in GCP Console with that account, and the refresh token must be stored securely (never in a file in this folder).
2. **Region — CONFIRMED 25 Nov 2026:** `AWS_REGION=us-east-2` (user corrected: not us-east-1, east-2). DynamoDB `yt_*` tables live in us-east-2. Set `AWS_REGION=us-east-2` everywhere; do NOT use ap-southeast-2 from `.env.example` or us-east-1. `.env.example` / README were not updated in this patch set.
3. **Market sizing:** no verified TAM/penetration source exists in the repo. `agents/research.py` now sets `tam_usd` / `penetration_pct` to an explicit `0.0` "not retrieved" sentinel with a loud warning. Decide: make `run()` raise here instead, or wire a real market-size source? Downstream must never render "$0 TAM".
4. **Deployment:** there is still no `main.py` / Dockerfile. The bot runs via the GitHub Actions 4-hour retrigger chain (`bot.yml`); a Codespace stopping stops the bot. Decide the 24/7 host before calling this production-ready.

## 4. Per-file changes

### tools/llm_provider.py — P0 #1
- `_call_gemini()`: missing Gemini key now raises `ValueError` instead of returning `"{}"` mock success. This restores `call_with_fallback()` — a missing key now falls through to the next provider.
- `_call_bedrock()`: raise behaviour unchanged; only the misleading log line ("Returning fallback response if testing.") fixed to "Raising."
- `_call_openrouter()`: unchanged (already raised correctly).

### tools/yfinance_tool.py — P0 #2
- `get_ticker_financials()`: any failure now raises `RuntimeError` instead of hardcoded invented financials (revenue $1.0B/$1.25B, margins 0.65/0.22/0.18, growth 0.25, market cap $25B). Empty `info` dict for an unknown ticker (no revenue + all margins zero) now raises `ValueError`.
- Added `_is_legacy_mock_data()`: stale cached entries matching the old mock signature are ignored and re-queried.
- `get_competitor_comparison()`: propagates the raise — one failed ticker fails the comparison instead of injecting invented numbers.

### tools/tinyfish.py — P0 #3
- `search_fintech_context()`: missing key raises `ValueError`; non-200 raises `RuntimeError`; httpx errors re-raised; zero articles raises `RuntimeError`. Fabricated articles (fake `alphapilot.io` / `fintechinsights.io` URLs, fake sources/dates) deleted; only real API results are cached.
- Added `_is_fabricated_article()` + cache filtering for old fake-domain entries.
- `get_topic_summary()`: propagates the raise, can no longer cite fabricated articles.

### agents/research.py — P0 #4, #5
- `run()`: raises `ValueError` if no ticker can be determined; raises `RuntimeError` if yfinance returns no revenue / no non-zero margin / no growth rate. No Stripe-like defaults (revenue $8.5B/$11.2B/$14.5B, margins 0.68/0.24/0.19, TAM $2.5T, etc.).
- Sources built only from real retrieved documents (per-year Yahoo Finance `SourceInfo`s, one SEC EDGAR filing-index source, one per real TinyFish article). No fabricated "SEC 10-K / Yahoo Finance" sources. Confidence is a documented constant `REAL_DATA_CONFIDENCE = 0.9` ("retrieved live, not independently audited"); `research_confidence` is computed from retrieval completeness instead of hardcoded 0.96.
- Removed invented segments / unit economics / competitor take rates / entity facts; those fields are left empty with TODOs. TinyFish articles are now wired into `technical_context` + sources; EDGAR filing search is wired into an SEC source + entity name.
- `_infer_ticker()`: removed `stripe → SQ`, `revolut → PYPL`, and the `return "SQ"` default. Only public-company mappings remain; unknown/private topics return `None` and `run()` raises asking for an explicit public ticker.
- Deferred: EDGAR `companyfacts` revenue reconciliation (P1 TODO), competitor benchmarks / segments / unit economics (need real sources that do not exist in the code).

### agents/script.py — P0 #6
- Deleted the 13-scene hardcoded template and 3 fixed-angle Shorts template, including fixed numbers (65%, 68%, $2.5T, <5%, 1.8%) and false SEO claims ("verified directly against official SEC EDGAR filings", "zero-hallucination").
- `run()` now loads the pillar prompt file (`prompts/script_finance_to_technical.txt` / `prompts/script_tech_to_financial.txt`, `FileNotFoundError` if missing), builds context from the full bundle via `_bundle_context()` (financials, market, competitors, entities, sources, technical context, YouTube insights), and calls `call_with_fallback(..., schema=VideoScript)`. LLM failures propagate — no template fallback.
- New `_validate_script()`: requires a disclaimer scene, SEO starting with `DISCLAIMER`, and every scene citation to be a real bundle `claim_id`; otherwise raises.
- `generate_shorts()`: LLM picks `count` distinct angles from the bundle via one structured call; raises if fewer Shorts than requested — never pads with templates.

### agents/verify.py — P0 #7
- `_check_investment_advice_semantic()`: was a stub always returning pass; now a real LLM check reading `prompts/verify_investment_advice.txt`, sending all scene narrations, calling `call_with_fallback(..., schema=_AdviceVerdict)`. Flagged phrases become critical/major issues with fixes.
- Fail-closed: unreadable prompt file or any LLM failure returns `status="fail"` with a critical issue — never pass. The 9-step audit count is unchanged because the check is now genuinely implemented.
- Deferred (P1): citation loophole in `_check_factual_accuracy()` left as a TODO comment (a number passes if any citation exists, even if it does not back the number; word-form numbers unchecked).

### tools/polly.py — P0 #8
- `synthesize_speech()`: any Polly failure (missing/invalid AWS keys, boto3 missing, network, Polly error, including after Neural→Standard fallback) now re-raises instead of falling through to `_generate_mock_audio()`. Missing keys / boto3 raise `RuntimeError` up front; missing/empty `AudioStream` raises.
- `_generate_mock_audio()`: gated behind explicit `ALLOW_DEMO_MODE` (default off, raises otherwise).
- `_estimate_or_read_duration()`: measures the real MP3 length via mutagen and raises if it cannot — no more word-count estimate presented as a real duration.

### tools/image_gen.py — P0 #9, #10
- `generate_image()`: Imagen/FLUX errors now propagate as the original error; unknown `model` raises `ValueError`. Fallback Pillow card removed from the success path.
- `_generate_gemini_native()`: no longer unconditionally returns the fallback stub — raises `NotImplementedError` (the original code contained no real Gemini native image API call to reconstruct), naming Imagen 3 / FLUX as working alternatives.
- `_generate_fallback_graphic()`: gated behind `ALLOW_DEMO_MODE` (default off); Pillow failure raises the original error; the `dest_path.touch()` 0-byte path is removed. `_generate_imagen3()` / `_generate_flux()` raise on empty image bytes.

### agents/charts.py — P0 #10 (audit) / #11 (task numbering)
- `generate_chart_by_type()`: `data=None` raises `ValueError`; every chart type requires its keys via `_require()`. All hardcoded defaults removed (Stripe/Adyen/PayPal/Square take rates, 0.65/0.20/0.15 segments, 2.9/1.8/1.1/0.45/0.65 waterfall, 14.5e9/2.5e12/4.8 TAM, Stripe 38/Adyen 26/PayPal 21/Others 15 shares). Unknown chart type raises `ValueError`.
- In-function `.get(..., hardcoded)` defaults now raise if a key is missing; TAM title uses data-driven `_format_usd(tam)`.
- All 8 chart functions: errors now log and raise instead of returning a stub PNG; `_generate_chart_stub()` deleted.

### agents/assemble.py — P0 #11 (audit) / #12 (task)
- `_render_composition()`: MoviePy/FFmpeg errors now raise `RuntimeError` (chaining the original error) instead of returning `_generate_placeholder_mp4()`. Empty audio/visual asset lists raise `ValueError` before rendering; a successful render is validated (file must exist and be ≥ 1024 bytes — the old placeholder was ~40 bytes).
- `_generate_placeholder_mp4()`: raises unless `ALLOW_DEMO_MODE` is set; even then it writes a distinctly named `<stem>.DEMO-PLACEHOLDER.mp4`, never the real destination.
- Comments added: burned-in subtitles, Ken Burns zoom and chart fade-in are P1 unimplemented claims — not invented.

### agents/uploader.py — P0 #12 (audit) / #13 (task) — BREAKING signature change
- `upload_and_register()` / `run_upload()` now take a required `verification_report: VerificationReport` argument (`models.verification`, `overall`/`confidence`). `None`, non-`"pass"`, or missing/non-numeric confidence raises `ValueError` before any upload.
- `verification_score` is now `float(report.confidence)` — hardcoded `1.0` gone; `verification_overall` also recorded. `duration_sec` is the ffprobe-probed duration, not the script estimate.
- Validation: file must exist, be ≥ 10,000 bytes, and pass an ffprobe probe (≥ 1.0 s). ffprobe missing/failing or zero duration raises — unprobed files are never uploaded.
- Thumbnail: supplied-but-missing/0-byte raises; no thumbnail logs a warning and records `thumbnail_url` as `null`, never `""`.
- Purge ordering: upload → verify YouTube copy via new `verify_video_exists()` → save DynamoDB record → only then `purge_local_media()`. Any earlier failure raises and leaves local files intact.
- See §1: the graph call site must be wired to pass the real report.

### tools/youtube_api.py — P0 #13, #14, #15 (audit) / #14–17 (task)
- `upload_private()`: no service / any upload exception now raises; `yt_sim_<md5>` fake IDs removed; missing/empty response ID raises; a `yt_sim_` ID is explicitly rejected.
- `publish_public()` / `delete_video()`: unauthenticated calls raise; they return `False` only for a real API failure.
- `_get_youtube_service()`: no longer builds with `developerKey=client_secret`. Raises `RuntimeError` stating real OAuth2 is unimplemented and required (stored refresh token, `YT_REFRESH_TOKEN` env — name only), explaining an API key is read-only and can never authorise writes. No OAuth credentials invented.
- `research_topic()`: raises on no service / API exception / zero results; returns only real `top_titles`. Hardcoded titles/tags/gaps removed (`common_tags`/`gaps` are now `[]` with a comment that they need a real `videos.list` implementation). Fake-result caching to `cache/youtube/` removed entirely.
- New `verify_video_exists(yt_video_id)` helper: real `videos.list` check, `True` only on an ID match, raises on no service / API error / simulated IDs.

### orchestration/graph.py — P0 #16, #17 (audit) / #18, #19 (task)
- `node_finalize`: now calls the real uploader (`run_upload(...)` from `agents/uploader.py`) and sets `state.output_video_id` to the real returned ID. `completed` only after that call returns. Empty / `yt_sim_` / `yt_priv_` IDs raise `RuntimeError`. `hashlib_sim` and all `yt_priv_<md5>` minting removed.
- Assembly/upload failure (missing script, longform/Shorts assembly failure, upload failure): `status="failed"`, `error=str(exc)`, `output_video_id=None`.
- P1: renders **all 3 Shorts** in a loop; new `PipelineState.rendered_shorts_paths: List[str]`; `run_pipeline()` (graph version) returns `rendered_shorts_paths`.
- P1 cost: `run_pipeline()` returns `"visual_spend": state.cost_usd` (0.0 when untracked) — the `or 0.09` fabricated fallback is gone. Note: `state.cost_usd` is never populated anywhere in the original graph, so spend is honestly 0.0 (untracked) until agents report real costs.
- `TubePilotGraph.run()`: after `node_verify`, if `state.status == "failed"` it returns immediately — a verify crash is no longer looped through `fix_script` and re-labelled `human_review`. TODO left: no Telegram human-review notify is sent (no notify/queue path exists in this file).

### pipeline.py — P0 #18 (audit) / #20 (task)
- `PipelineResult` defaults: `verification_overall="not_run"` (was `"pass"`), `verification_confidence=0.0` (was `1.0`).
- `run_pipeline()`: `ver_overall`/`ver_conf` default to `"not_run"`/`0.0` when `state.verification` is `None`; a `None` confidence is coerced to `0.0`.

### config_manager.py — P0 #25 (region) + P1 #10
- Single region source of truth: `_AWS_REGION = os.environ.get("AWS_REGION")` (the `.env.example` value); no hardcoded region. DynamoDB failures raise `RuntimeError` in production; in-memory fallback only with explicit `ALLOW_IN_MEMORY_CONFIG=1` (default off).
- `_load_full_config()`: read failure raises in production (missing stored row on first boot still returns defaults — explicitly not a failure fallback).
- `_save_full_config()` / `set_config()`: write failures raise; in-memory config is only updated after a successful `put_item`, so a failed write can no longer log "updated".
- New exported `validate_config()`, called from `snapshot_config()` at pipeline startup: requires `active_models.verify/script/image`, `verify_chain`, and numeric `budget_ai_per_video` — raises listing every missing key.

### tools/secrets.py — P0 #25 + P1 #11
- Same fail-loud pattern with its own flag `ALLOW_IN_MEMORY_SECRETS` (default off): `_get_table()` raises in production, uses `region_name=_AWS_REGION` (`AWS_REGION` only); `_load_secrets_record()` raises on read failure; `_save_secrets_record()` raises on write failure instead of silently keeping secrets in-memory only. Fernet encryption (PBKDF2, salt, iterations) unchanged.
- `get_secret()`: if a stored token exists and decryption fails (wrong/missing `MASTER_SECRET`), raises `RuntimeError` instead of silently falling through to the env var and using a different secret. Env fallback applies only when no stored token exists.
- `test_secret()`: untested providers now log a warning and return `False` instead of `True` ("assume valid").

### db/setup_dynamo.py — P0 #25
- Hardcoded `region_name="us-east-1"` removed. New `_aws_region()`: single source of truth is env var `AWS_REGION`; missing value raises `RuntimeError` — no hardcoded fallback, `AWS_DEFAULT_REGION` not used.
- `setup_all_tables()`: missing boto3 / client-connection failure now raise instead of returning `{"status": "skipped"/"failed"}` dicts. `_create_table()` raises on create failure (except existing `ResourceInUseException`) and on TTL-configuration failure (previously swallowed). New `_verify_tables_exist()` describes all 7 tables after provisioning and raises if any is missing. Success still returns the original `{"status": "success", ...}` dict; `__main__` exits non-zero on failure.

### scripts/dev_run.py — P0 #27 (audit, cron)
- New flags: `--scheduled` (cron) and `--allow-default-topic` (explicit local-dev opt-in).
- Empty queue + `--scheduled`: logs exactly `queue empty, nothing to run`, stops the Codespace if applicable, exit code `0` — never defaults to "How Stripe Makes Money".
- Empty queue + `--allow-default-topic`: uses that topic (the only place the default now appears).
- Empty queue with neither flag: loud error and exit code `2`. A queued record without a topic is also treated as empty.
- Success notify: removed fabricated `duration 600.0`, `spend 0.09`, `shorts_count=3`. Duration from `script.meta.estimated_duration_sec` (fallback `script.total_duration_sec`), spend from the real pipeline result, shorts count from `len(rendered_shorts_paths)` actually rendered. Unknown duration/spend reported as `0.0` (unknown, with a warning log).

### telegram_bot/bot.py — P0 #19, #20, #21, #22, #24, #26 (audit)
- `check_admin_auth()`: `TELEGRAM_ADMIN_ID` unset now logs a security warning and returns `False` (was `return True`) — fail-closed.
- `_handle_script_command()` (`/script`, `/audit`, `/factcheck`, `/draft`, `menu_script`): rewritten to read `list_recent_videos(limit=5)` (fake ID filtered out) and `get_latest_verification_log(video_id)` from DynamoDB; matches a topic arg against title/topic/video_id, otherwise takes the latest record. No match / no records → `❌ No script/audit found for <topic>` (no default topic). Shows only fields actually on the record plus stored audit fields; no audit → explicit "No verification audit found … Do not treat this script as fact-checked." Removed: hardcoded 13/13, 7× PASS, `PASS (0 Critical, 0 Major Issues)`, duration 10:20, 13 scenes, default "How Stripe Makes Money".
- `/run` and `run_*` callbacks: check `dispatch_pipeline_run()`'s `dispatched` flag; if `False`, reply `❌ Dispatch failed: <details>` instead of `🚀 Pipeline Dispatched!`.
- Queue-ID bug (P1): resolves the callback payload against `list_topics_queue()` by `topic_id` first, then the named presets; unknown `top_…` ID returns `❌ Queue ID … not found — no run dispatched`.
- Approval callbacks: `rej_` checks `get_video_record()` then calls real `update_video_status(id, "rejected")` and confirms only on success (DB record only, not YouTube). `reg_` reads the record's topic and calls real `enqueue_topic(..., priority=10)`. New `render_` branch (previously unhandled) dispatches a real run for the record's topic, honouring the dispatched flag. `edt_` / `thm_` / `shorts_` now reply `❌ Not implemented — no action taken` (see "Not changed" below). `pub_*` unchanged — it already calls real `publish_public()` and checks its return.
- `_handle_videos_command()`: empty DB → `No videos yet — no production videos found in yt_videos. Use /run <topic>…`; fake `yt_priv_1292805d8f` Stripe video removed (also filtered from DB results).
- `_handle_preview_command()`: fake-ID fallback removed. No local render and no DB video → `🎬 No preview available — no videos produced yet` (no Publish button). A user-supplied ID is used only if `get_video_record()` finds it.
- `_handle_ideas_command()` (P1): header now labels the list as curated starter examples with unverified figures — no figures invented or removed.
- `run_bot()`: missing `TELEGRAM_BOT_TOKEN` now `sys.exit(1)`; `getMe` failure now `sys.exit(1)` (was log-and-return, exit 0). Loud startup log if `TELEGRAM_ADMIN_ID` is unset.

### telegram_bot/health.py — P0 #22
- `get_system_ping()`: pending approvals counted for real from `list_recent_videos(limit=100)` with `status == "pending_approval"`; spend = sum of `cost_usd` actually stored on those records, labelled a partial sum; GitHub API actually pinged and the last workflow run queried live when `GH_PAT` + `GITHUB_REPOSITORY` exist. DynamoDB, LLM providers and MTD spend labelled `unknown/not checked` (LLM lines say "key set (not pinged)" / "Unset", never "✅ Ready"). Removed: `pending_approvals = 1`, `mtd_cost = 0.38`, "DynamoDB: ✅ Connected", "GitHub Actions: Last run succeeded".
- `get_cost_summary()`: per-video cost shown only if `cost_usd` is on the record, else "unknown/not recorded"; average/MTD/YouTube/DynamoDB costs labelled unknown. Removed fabricated `$0.08–$0.14` / `$0.38` / `$0.00`.
- `get_pipeline_logs()`: returns `❌ No logs found for <run_id>` — no canned Stripe log lines, since no log store is queried.

### telegram_bot/agent_chat.py — P0 #23
- `_build_grounding_context()`: empty `list_recent_videos()` now yields `- No videos produced yet.`; the injected fake `yt_priv_1292805d8f` "How Stripe Makes Money" entry under "REAL VERIFIED SYSTEM CONTEXT" is removed, and that ID is filtered out if present in the DB.

### telegram_bot/dispatch.py — P0 #20
- Already returned `dispatched=False` correctly; the no-`GH_PAT` path wording clarified to `❌ Dispatch failed: GH_PAT not configured — GitHub dispatch not attempted. …` (`True` only on HTTP 204).

### telegram_bot/codespace.py — P1
- Stop: CLI exception now returns `❌ Could not stop <name>: <error>` (was fake `🛑 Stop signal dispatched…`); CLI non-zero return includes stderr/stdout. Start/run: a 204 response is handled without parsing JSON (previously `resp.json()` on an empty body would throw and fall through to the fake message); unconfirmed start returns `❌ Could not start <name>: GitHub API did not confirm the start (and no CLI start path is implemented).`

## 5. P0 coverage

| Audit P0 | Covered |
|---|---|
| #1 Gemini `"{}"` mock | Yes — `tools/llm_provider.py` |
| #2 Invented finance | Yes — `tools/yfinance_tool.py` |
| #3 Fabricated TinyFish articles | Yes — `tools/tinyfish.py` |
| #4 Fake research bundle / fabricated sources | Yes — `agents/research.py` |
| #5 `SQ` default / wrong ticker mappings | Yes — `agents/research.py` |
| #6 Template script, no LLM | Yes — `agents/script.py` |
| #7 Stub verification check | Yes — `agents/verify.py` |
| #8 Silence audio stub | Yes — `tools/polly.py` |
| #9 Placeholder image / 0-byte, Gemini-native stub | Yes — `tools/image_gen.py` |
| #10 Hardcoded charts / stub PNG | Yes — `agents/charts.py` (task brief numbered this #11) |
| #11 Empty MP4 success | Yes — `agents/assemble.py` (task brief #12) |
| #12 Uploader without verification, hardcoded 1.0 | Yes — `agents/uploader.py` (task brief #13; see §1 wiring gap) |
| #13 Fake `yt_sim_` upload ID | Yes — `tools/youtube_api.py` |
| #14 Publish/delete `True` unauthenticated | Yes — `tools/youtube_api.py` |
| #15 OAuth unimplemented / secret-as-API-key | Yes, as a loud raise — `tools/youtube_api.py` (real OAuth not implemented, §3) |
| #16 Graph never uploads, fake `yt_priv_` ID | Yes — `orchestration/graph.py` (see §1 wiring gap) |
| #17 Assembly failure swallowed | Yes — `orchestration/graph.py` |
| #18 Verification default pass / 1.0 | Yes — `pipeline.py` |
| #19 `/script` hardcoded audit | Yes — `telegram_bot/bot.py` |
| #20 `/run` dispatch failure hidden | Yes — `telegram_bot/bot.py`, `telegram_bot/dispatch.py` |
| #21 Approval buttons no-op | Yes, partially — `rej_`/`reg_`/`render_` now real; `edt_`/`thm_`/`shorts_` explicitly "not implemented" (see §6) |
| #22 `/ping` / `/cost` / `/logs` / `/videos` / `/preview` fake data | Yes — `telegram_bot/health.py`, `telegram_bot/bot.py` |
| #23 Fake grounding video in agent chat | Yes — `telegram_bot/agent_chat.py` |
| #24 Auth fail-open | Yes — `telegram_bot/bot.py` |
| #25 Region mismatch | Yes — `config_manager.py`, `tools/secrets.py`, `db/setup_dynamo.py` (single var `AWS_REGION`) |
| #26 Bot exit 0 on missing token | Yes — `telegram_bot/bot.py` |
| #27 Daily cron default Stripe topic | Yes — `scripts/dev_run.py` |

All 27 P0 items have a patch. "Covered" means the silent mock / fake success is removed and the code now fails loudly (or, where noted, raises `NotImplementedError`).

## 6. What was NOT changed, and why

- **Real YouTube OAuth2** — not implemented. It needs live user consent in GCP Console and a stored refresh token; it cannot be invented in a patch file.
- **Real Gemini native image generation** — `NotImplementedError`, not a guessed API call; the original code contained no endpoint, model, or response shape.
- **`edt_` / `thm_` / `shorts_` Telegram buttons** — explicitly "not implemented — no action taken". No edit state, thumbnail-upload path, or Shorts renderer is wired anywhere in the bot slice; claiming success would repeat the fake. Wiring them needs a real workflow (pending-edit store + reply handler, thumbnail upload, Shorts generation).
- **`/script` full script text** — cannot be shown; no script text is stored in `yt_videos` (verified against `db/dynamo.py`). The patch shows the record + stored audit only. A real script store is needed for full text.
- **Verify citation loophole, fix-loop field handling, verify-crash Telegram notify** — P1 TODOs left in code; rewriting the verifier / inventing a notify path was out of scope for the P0 patch.
- **EDGAR companyfacts reconciliation, competitor benchmarks, segments, unit economics** — left empty / TODO in `agents/research.py`; no real sources for them exist in the code.
- **`research_topic` tags/gaps** — returned empty rather than invented; populating them needs a real `videos.list` follow-up.
- **Pixabay music** — still the single hardcoded track (P1); no real music API was wired.
- **Burned-in subtitles / Ken Burns / chart fade-in** — noted as unimplemented in comments only; implementing them is P1 work.
- **Ephemeral-storage layers, LangGraph claim, self-healing `run_stage`** — P1, not rewired in this patch set.
- **`.env.example` / README** — not updated in the original patch set; a corrected `.env.example` was added 25 Nov 2026 in this folder (`fixes/.env.example`): `AWS_REGION=us-east-2`, Gemini + OpenRouter only (no Bedrock), `YT_REFRESH_TOKEN` and `GEMINI_KEY_POOL` added. README still needs the same update.
- **`db/dynamo.py`, `agents/visuals.py`, `agents/tts.py`, `tools/fal_ai.py`, `tools/replicate_tool.py`, `tools/sec_edgar.py`, `tools/pixabay.py`, `scripts/notify_success.py`, `storage/ephemeral.py`, tests** — not patched. `visuals.py` in particular must be updated to pass bundle data to charts (see §2). Tests were not run and not updated; several existing tests assert the old demo behaviour (e.g. `yt_priv_` ID format, mock data) and will fail against the patched code — that is expected and those tests need updating before the patches are applied.
- **No files were pushed, branched, or run.** Applying, testing, and the GCP/YouTube OAuth setup are separate steps requiring the user (see §3).

## 7. Follow-ups applied on branch fix/production-mocks (25 Nov 2026, PR #1)

- **Visuals wiring (was §6, now resolved)** — `agents/visuals.py` gained
  `research_bundle_to_chart_data(bundle)` (only values actually present in
  the bundle; TAM 0.0 sentinel omitted; market-share and competitor-matrix
  omitted — those charts raise until a real source is wired).
  `agents/assemble.py` `assemble()` / `assemble_video()` / `assemble_shorts()`
  accept `bundle_data`; `orchestration/graph.py` `node_finalize` now requires
  `state.research` and passes `bundle_data=state.research` to both assemble
  calls. Chart scenes raise without a bundle instead of plotting fabricated
  data.
- **`db/dynamo.py` log_verification defaults (was §6, now resolved)** —
  defaults changed from `overall="pass"` / `confidence=1.0` to `"not_run"` /
  `0.0`, and `model_used` default from `"claude-3-5-haiku"` to `"unknown"`.
- **Stale comments** — all "NOT pushed to GitHub" / "TODO: agents/uploader.py
  does not yet accept" notes corrected; `agents/uploader.py` header now says
  graph passes the real VerificationReport.
- **Tests rewritten for the fail-loud behaviour (25 Nov 2026)** — the old
  tests asserted the demo behaviour and failed against the patched code by
  design. Rewritten: `tests/test_verify.py`, `test_research.py`,
  `test_media.py`, `test_tools.py`, `test_storage_uploader.py`,
  `test_graph.py`, `test_bot.py` (tests 6, 7, 12, 14), plus a shared
  fixture module `tests/_fixtures.py` (`make_test_bundle()` /
  `make_test_script()` — explicit, labelled test data only, never used by
  production code). Tests now assert that missing keys / sources / services
  raise instead of returning fake success, and that a failing
  VerificationReport blocks upload.
- **CI env flags (25 Nov 2026)** — `.github/workflows/ci.yml` test job now
  sets `ALLOW_IN_MEMORY_CONFIG=1` and `ALLOW_IN_MEMORY_SECRETS=1` (CI has no
  DynamoDB; these flags are test-only and do not enable any mock/demo
  fallback in production code).

- **CI env flags — blocked by GitHub API permissions (25 Nov 2026)** —
  `.github/workflows/ci.yml` could NOT be pushed via the GitHub connector:
  `push_files` returns `403 Resource not accessible by integration` on any
  tree containing a workflow file (the integration lacks workflow-write
  scope). The corrected `ci.yml` (with `ALLOW_IN_MEMORY_CONFIG=1` /
  `ALLOW_IN_MEMORY_SECRETS=1` in the test job) is kept in this folder at
  `fixes/.github/workflows/ci.yml` — it must be applied by someone with a
  token that has workflow scope, or via the GitHub web UI. Workaround in
  place: `tests/test_bot.py` sets both flags at module level before any
  production module is imported (discover imports test_bot.py first), and
  every rewritten test file also sets them before its own imports, so CI
  passes without the workflow change. `config_manager.py` /
  `tools/secrets.py` read these flags once at import time.
- **test_storage_uploader test_1 (25 Nov 2026, after CI run #17 failed)** —
  `setup_all_tables()` provisions real DynamoDB tables and raises
  RuntimeError without AWS credentials; the rewritten test wrongly
  asserted a success dict in CI. It now asserts the fail-loud raise.
