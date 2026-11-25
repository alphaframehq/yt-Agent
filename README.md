# TubePilot AI (yt-Agent) — README

> **Repo:** `alphaframehq/yt-Agent`
> **Pinned audit commit:** `c39f42541b8e784dcf41c09583dc57227be07667` (`main`, 25 Nov 2026)
> **Status, 25 Nov 2026: NOT production-ready — do not run a production pipeline yet.**
> **Update:** PR #1 (`fix/production-mocks`) **MERGED 25 Nov 2026**,
> merge commit `8212f7623934226559b18c3b2e1b67a17552f5d5`, 35 files,
> CI Tests run #18 PASSED (1m 48s). The fail-loud fixes below are now
> on `main`. YouTube OAuth2 and hosting are still
> open — see `docs/GAPS.md`. Agnes integration is added in this PR.
> **Update 25 Nov 2026:** DynamoDB removed — all non-secret data now
> lives in Google Sheets (`db/sheets.py`, 8 tabs); secrets live in GitHub
> repo secrets only. Live DynamoDB data could not be migrated (no AWS
> credentials exist anywhere). See `docs/SETUP.md` §3.

## What TubePilot AI does

TubePilot AI is an autonomous Tech–Finance YouTube pipeline, controlled by a
Telegram bot:

```
research → script → verification → media / render → YouTube (private upload)
```

- **Claimed cadence (README, unverified in production):** 1 long-form video
  (8–12 min) + 3 Shorts per day.
- **Topics:** tech-finance — public companies only. Private companies
  (Stripe, Revolut, …) have no public ticker and must **not** be silently
  mapped to another company's ticker.
- **Control:** Telegram bot (`/run`, `/script`, `/audit`, `/ping`, `/cost`,
  `/videos`, `/preview`, approvals). Admin-only — see "Security" below.
- **Money / facts:** every number in a script must come from a real retrieved
  source (yfinance / SEC EDGAR / TinyFish). The pipeline must never invent
  revenue, margins, TAM, take-rates or sources. If a number cannot be
  retrieved, the run **fails loudly** instead of publishing a plausible lie.

## Honest production status (25 Nov 2026)

### What the audit found

The 25 Nov 2026 audit
(`~/workspace/yt-agent-audit/yt-agent-production-audit-list.md`, 27 P0 items)
found the pipeline was **a demo that reported success**: it could complete
with no keys, no LLM, no research, no TTS, no images, no render and no
upload — and still report a verified, published video with a fake ID.

### What PR #1 fixed (branch `fix/production-mocks`, **MERGED 25 Nov 2026**,
merge commit `8212f7623934226559b18c3b2e1b67a17552f5d5`, 35 files,
+3211/−1239, CI Tests run #18 PASSED)

All 27 P0 items are patched to **fail loudly** instead of faking success:

| Area | Before (demo) | After PR #1 (patched) |
|---|---|---|
| LLM | Missing Gemini key returned `"{}"` | Raises; fallback to OpenRouter works |
| Research | Invented Stripe-like financials, fake articles, `SQ` default ticker | Raises on missing/invalid data; sources only from real documents |
| Script | 13 fixed scenes, no LLM call, prompt files never read | LLM call with prompt file + validated bundle; citations checked |
| Verification | Investment-advice check always passed | Real LLM check; LLM failure = `fail`, never pass |
| TTS | 1-sec silence MP3 on Polly failure | Raises (demo stub only behind `ALLOW_DEMO_MODE`) |
| Images | Pillow placeholder card / 0-byte file | Raises; see the Agnes wiring in tools/image_gen.py |
| Charts | Hardcoded Stripe/Adyen/PayPal data, stub PNG | Bundle data mandatory; raises without it |
| Render | ~40-byte empty MP4 reported as success | Raises; rendered file validated (≥ 1024 bytes) |
| Upload | Fake `yt_sim_` / `yt_priv_` IDs, hardcoded verification 1.0 | Real uploader, real `VerificationReport` required, fail = refuse |
| Telegram | Hardcoded Stripe audit, fake $0.38 cost, fail-open auth | Real Google Sheets reads, fail-closed auth, dispatch failure reported |
| Region | `us-east-1` / `ap-southeast-2` / `us-east-1` in three places | Single source of truth: `AWS_REGION=us-east-2` |
| Cron | Empty queue silently made a "How Stripe Makes Money" video | Scheduled run with empty queue exits cleanly, runs nothing |

Full per-file notes: `docs/PRODUCTION_FIXES.md`.

### What still does NOT work — do not call this production-ready

1. **YouTube OAuth2 is NOT implemented in code.** `tools/youtube_api.py`
   `_get_youtube_service()` raises `RuntimeError` on every write, by design.
   A refresh token exists (GCP, 25 Nov 2026 — see `docs/SETUP.md`), but the token
   alone does not make uploads work. No upload, publish, delete or
   authenticated read will succeed until real OAuth2 is implemented.
2. **Telegram `agent_chat.py` INTENT 2** still replies
   "Production Run Dispatched!" unconditionally — it does not check the
   dispatch flag the way `bot.py` `/run` now does.
3. **No 24/7 host.** There is no `main.py` / Dockerfile / Procfile. The bot
   runs on a GitHub Actions 4-hour retrigger chain; a Codespace stopping
   stops the bot.
4. **Tests were rewritten for fail-loud behaviour** (PR #1 follow-up) and
   **CI Tests run #18 PASSED on the merged PR (1m 48s)** — but the
   **CI workflow file itself could not be pushed** via the GitHub connector
   (`403` on workflow files — needs a token with workflow scope, or the
   GitHub web UI). The corrected `ci.yml` is kept locally at
   `~/workspace/yt-agent-audit/fixes/.github/workflows/ci.yml`. See `docs/GAPS.md`.
5. **Agnes AI integration is added in this PR** (`tools/agnes_ai.py` +
   wiring in `tools/image_gen.py`) — it has **not** been tested
   end-to-end or verified against a live Agnes call. The image model
   was verified live on 25 Nov 2026 (a real 9:16 1K PNG was generated);
   video generation is untested. The free-trial price ($0) has **no
   stated expiry/limit** — re-check before relying on it.
6. **Market sizing has no real source.** `tam_usd` / `penetration_pct` are an
   explicit `0.0` "not retrieved" sentinel. Downstream must never render
   "$0 TAM". See `docs/GAPS.md`.
7. **Pixabay music is still one hardcoded track** (P1, unfixed). Burned-in
   subtitles, Ken Burns zoom and chart fade-in are still unimplemented
   claims in `assemble.py`. EDGAR `companyfacts` reconciliation is still
   TODO. Verify-citation loophole (a number passes if *any* citation
   exists) is still open.

**Production-ready requires, at minimum:** PR #1 merged + CI green,
YouTube OAuth2 implemented, refresh token stored **and** verified with a
real private upload, Agnes (or another image/video provider) verified with
a real generation, and one full end-to-end run (research → upload-private)
observed succeeding — with a failing run observed failing loudly.

## Architecture

```
telegram_bot/        Telegram bot: bot.py, agent_chat.py, dispatch.py,
                     health.py, codespace.py
orchestration/       graph.py — pipeline state machine (sequential,
                     NOT LangGraph despite the README claim)
pipeline.py          Second run_pipeline entry point (different contract —
                     see docs/GAPS.md; unify before calling this stable)
agents/              research.py, script.py, verify.py, charts.py,
                     visuals.py, assemble.py, uploader.py, tts.py
tools/               llm_provider.py (Gemini + OpenRouter),
                     yfinance_tool.py, tinyfish.py, sec_edgar.py,
                     polly.py, image_gen.py, agnes_ai.py (NEW, this PR),
                     fal_ai.py, replicate_tool.py, pixabay.py,
                     youtube_api.py, secrets.py
db/                  sheets.py, setup_sheets.py (8 tabs in one spreadsheet)
config_manager.py    Config from Google Sheets yt_config tab (fail-loud in prod)
scripts/             dev_run.py — daily cron entry point
prompts/             Script / verify prompt files (actually read now)
.github/workflows/   bot.yml (4h retrigger), daily.yml (04:00 UTC),
                     ci.yml
```

### Daily flow (intended)

1. `daily.yml` cron (04:00 UTC) runs `scripts/dev_run.py --scheduled`.
   Empty queue → logs `queue empty, nothing to run`, exits 0, runs nothing.
2. A queued topic is researched (yfinance + TinyFish + EDGAR), scripted by
   LLM (Gemini primary, OpenRouter fallback), verified (9-step audit,
   incl. real investment-advice LLM check), rendered (Polly TTS + images /
   charts / Agnes clips + MoviePy/FFmpeg assembly).
3. `node_finalize` requires a passing `VerificationReport`, uploads as
   **private**, verifies the YouTube copy exists, writes the Google Sheets
   record, and only then purges local media.
4. Telegram approval (`pub_*`) publishes private → public. Rejection,
   re-render and re-enqueue are real DB/dispatch actions; Edit / Thumbnail /
   Shorts buttons reply "not implemented — no action taken".

### Cost / spend honesty

- Spend is **untracked (0.0) until agents report real costs**. Any cost
  figure shown by `/cost` or `/ping` that is not a sum of `cost_usd` stored
  on Google Sheets records is labelled unknown. Never quote $0.08–$0.14/video
  or $0.38 MTD — those were fabricated.

## Configuration — the short version

Single source of truth for region: **`AWS_REGION=us-east-2`**.
LLM: **Gemini AI Studio (free, primary) + OpenRouter (fallback). NO Bedrock.**
Media: **Agnes AI** (free Angel trial, Flash models) for images + short
video clips; Imagen 3 / FLUX remain as fallbacks; FAL for longer video.

Full step-by-step: **`docs/SETUP.md`**.
Corrected env template: **`.env.example`**.
Every remaining gap: **`docs/GAPS.md`**.
Agnes code: **`tools/agnes_ai.py`** (image + video client) and the wiring in **`tools/image_gen.py`**.

## Security

- `TELEGRAM_ADMIN_ID` is **mandatory**. If unset, the bot denies everyone
  (fail-closed). Never run without it.
- Never put a real secret in a file, a log, chat, or a repo. Secrets live
  in GitHub repo secrets (env) only — never in Google Sheets. Secret names
  only — see `docs/SETUP.md` § "Secrets table".
- `ALLOW_DEMO_MODE` is **dev-only**. Production must never set it.
  (The old `ALLOW_IN_MEMORY_CONFIG` / `ALLOW_IN_MEMORY_SECRETS` flags no
  longer exist — tests mock the Google Sheets client instead.)
- The old `.env.example` on `main` showed `<redacted>` values in one read.
  That was tool-side redaction of empty values, **not real secrets**
  (confirmed: the file on disk has empty values). No rotation needed
  for that file.

## Growth / Shorts — honest note

Agnes clips (5–10 s, Flash) + hooks/captions/pacing are wired into the
plan in `docs/GAPS.md` § "Video editing + hooks". **No subscriber number is
promised.** 1M subscribers in a month is not a deliverable of this setup;
growth depends on content quality, retention, niche, consistency and
distribution — none of which a pipeline config can guarantee.

## Files in this folder

| File | Purpose |
|---|---|
| `README.md` | This file — what it is, honest status, architecture |
| `docs/SETUP.md` | Step-by-step setup from scratch |
| `docs/GAPS.md` | Every remaining gap, prioritised, + next steps |
| `.env.example` | Corrected env template (us-east-2, Gemini+OpenRouter, Agnes) |
| `tools/agnes_ai.py` | NEW Agnes image + video client (fail-loud) |
| `tools/image_gen.py` | Wiring: Agnes as image provider when configured |
