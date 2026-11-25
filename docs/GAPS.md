# TubePilot AI — Remaining Gaps (kami) — 25 Nov 2026

> Prioritised. "Done" means verified live, not "patched
> locally". Nothing here is production-ready until the P0 section is
> empty. No subscriber number is promised anywhere in this file.

## P0 — Must fix before any production run

### 1. YouTube OAuth2 — IMPLEMENTED in code (PR #3), live verification still required
- **File:** `tools/youtube_api.py::_get_youtube_service()`
- **State:** implemented 25 Nov 2026 (PR #3, branch `feat/youtube-oauth2`).
  It builds real `google.oauth2.credentials.Credentials` from
  `YT_CLIENT_ID` + `YT_CLIENT_SECRET` + `YT_REFRESH_TOKEN`, refreshes
  the access token, and passes `credentials=...` (never `developerKey`)
  to `build("youtube", "v3", ...)`. Missing creds or a rejected refresh
  token raise loudly; `upload_private()` does a real resumable
  `videos.insert` (private by default) and returns only the real API
  video ID; the title is required from the script bundle. New tests in
  `tests/test_youtube_oauth.py` mock the Google client at the test
  layer only. The old code passed `YT_CLIENT_SECRET` as `developerKey`
  — wrong; an API key is read-only and can never authorise writes.
- **What is ready:** GCP project `tubepilot-ai-515805`, YouTube Data API
  v3 enabled, OAuth client "TubePilot Desktop", refresh token obtained
  25 Nov 2026, `channels.list` verified live in the Codespace.
- **What is still missing — NOT production-ready until done:** a real
  private-upload end-to-end test (SETUP.md §9 step 6): one successful
  private upload observed, and one failing run observed failing loudly.
  No video was uploaded by PR #3 itself.
- **Token storage: done + verified 25 Nov 2026.** `YT_CLIENT_ID` /
  `YT_CLIENT_SECRET` / `YT_REFRESH_TOKEN` are stored as GitHub
  Codespaces secrets and in the Codespace `.env`; verified three ways
  with `channels.list mine=true` (all returned channel Alpha Frame).
  A new client secret was created in GCP (Google no longer shows the
  old one); the old secret stays enabled.
- **Deadline pressure:** Testing-mode refresh token expires ~2 Dec 2026
  (7 days from 25 Nov). Either publish the OAuth app (Google
  verification, takes days) or re-issue weekly — see SETUP.md §1a.

### 2. Telegram agent_chat INTENT 2 replies "Dispatched!" unconditionally
- **File:** `telegram_bot/agent_chat.py`
- **State (CHANGES.md §2):** still replies "Production Run Dispatched!"
  without checking the dispatch flag. `bot.py` `/run` was fixed; this
  path was not.
- **Fix:** same dispatched-flag check as `bot.py` — `False` →
  "❌ Dispatch failed: <details>", no run claimed.

### 3. Agnes integration is local-only, unverified live
- **Files:** `tools/agnes_ai.py`, `tools/image_gen.py` (this PR)
- **State:** written 25 Nov 2026, `py_compile` syntax-checked only.
  Applied to the repo by this PR, never called against the live Agnes
  API from the repo. Image generation was verified live on 25 Nov 2026
  (a real 9:16 1K PNG, 1,368,632 bytes, was generated with
  `agnes-image-2.5-flash`); video generation is untested.
- **Unverified details (verify before first live call):**
  - Exact JSON fields for video-create `size` / `duration`
    (docs: https://www.agnes-ai.com/en/docs/agnes-video-25-flash).
  - Poll endpoint shape: reported as
    `GET /agnesapi?video_id=<ID>&model_name=<model>` — confirm the
    root (the patch strips a trailing `/v1` from the base URL) and the
    response fields for status / video URL.
  - Image response: patch handles both `data[0].b64_json` and
    `data[0].url` (OpenAI-compatible) — confirm which Agnes returns.
- **Free-trial caveat:** Angel Trial $0 for Flash models has **no stated
  expiry/limit**. Re-check billing/docs before relying on it; if it
  ends, calls fail (balance $0.00) — which is correct fail-loud
  behaviour, but means clips stop until you top up or switch provider.
- **Fix:** run SETUP.md §9 steps 4–5 from the repo code in this PR,
  check the Agnes usage page moves from 0.

### 4. Deployment / 24-7 hosting undecided — BLOCKER for "production"
- **State:** no `main.py` / Dockerfile / Procfile. Bot runs via
  `.github/workflows/bot.yml` 4h retrigger chain; pipeline via
  `daily.yml` 04:00 UTC. A Codespace stopping stops the bot.
- **Decide:** a real host (VPS / Fly.io / Railway / dedicated runner /
  etc.), how secrets reach it, how restarts/logs are handled, and who
  pays for it. Until decided, "production" means "GitHub Actions cron
  that may silently stop".

### 5. CI workflow file could not be pushed (PR #1 follow-up — still open)
- **State:** `.github/workflows/ci.yml` (test job with
  `ALLOW_IN_MEMORY_CONFIG=1` / `ALLOW_IN_MEMORY_SECRETS=1`) could **not**
  be pushed via the GitHub connector — `403 Resource not accessible by
  integration` on any tree containing a workflow file (integration
  lacks workflow-write scope). The corrected file is kept at
  `~/workspace/yt-agent-audit/fixes/.github/workflows/ci.yml`.
  **PR #1 itself merged and CI Tests run #18 PASSED (1m 48s)** via the
  in-test workaround below — the missing piece is only the workflow
  file on `main`.
- **Workaround in place:** `tests/test_bot.py` sets both flags at module
  level, and every rewritten test file sets them before its imports, so
  CI passes without the workflow change.
- **Fix:** apply the `ci.yml` via a token with workflow scope or the
  GitHub web UI, then remove the workaround only if CI stays green.
  Verify CI is actually green on `main` after the merge — do not assume.

### 6. Tests: rewritten, CI run #18 passed on the merged PR
- **State:** 7 test files rewritten for fail-loud behaviour
  (`test_verify`, `test_research`, `test_media`, `test_tools`,
  `test_storage_uploader`, `test_graph`, `test_bot`) + shared fixtures
  `tests/_fixtures.py`. One fix applied after CI run #17 failed
  (`test_storage_uploader` now asserts the fail-loud raise from
  `setup_all_tables()` without AWS credentials). **CI Tests run #18
  PASSED (1m 48s) and PR #1 merged (8212f762).**
- **Remaining check:** confirm the next CI run on `main` (after any
  further push, e.g. the Agnes PR) is also green. If a test still
  asserts old demo behaviour, update the test — never restore a mock
  to make a test pass.

## P1 — Serious, fix before scaling

### 7. Market sizing has no real source (TAM sentinel)
- `agents/research.py` sets `tam_usd` / `penetration_pct` to explicit
  `0.0` "not retrieved" with a loud warning. `agents/visuals.py`
  `research_bundle_to_chart_data()` omits TAM, market-share and
  competitor-matrix charts — those raise until a real source is wired.
- **Decide:** make `run()` raise when TAM is requested but unretrieved,
  or wire a real market-size source. **Never** render "$0 TAM" or a
  $2.5T invented TAM in a script/chart.

### 8. EDGAR companyfacts reconciliation — TODO
- `agents/research.py`: EDGAR filing search is wired to a source +
  entity name, but `companyfacts` revenue reconciliation is still
  TODO. yfinance numbers are therefore not independently checked
  against SEC filings, despite the old script claiming "verified
  directly against official SEC EDGAR filings" (that claim was removed).

### 9. Verify citation loophole + dead checks
- `agents/verify.py`: a number passes if *any* citation exists, even
  if it does not back the number; word-form numbers ("sixty-eight
  percent") are never checked. `_check_contradictions()` does not
  compare revenue mentions; `_check_narration_quality()` claims an LLM
  check but is regex only. Verify-crash → "human_review" in the graph
  sends **no** Telegram message and persists **no** queue item.

### 10. Pixabay music — still one hardcoded track
- `tools/pixabay.py::search_background_music()` always returns the same
  single 2022 track, no API call. `download_media()` failing returns an
  empty file as success. Wire a real music search (Pixabay API with
  `PIXABAY_KEY`, or another licensed source) or remove background music
  from the render path and say so in the output.

### 11. Assembly claims unimplemented
- `agents/assemble.py`: burned-in subtitles, Ken Burns zoom and chart
  fade-in are promised but not implemented (noted in comments only).
  Background music missing is silently omitted; a 10-sec stock clip is
  stretched to the full scene duration. Implement or remove the claims
  — a rendered video must match what the pipeline says it contains.

### 12. Cost tracking is 0.0 (untracked)
- `orchestration/graph.py`: `state.cost_usd` is never populated, so
  `visual_spend` is honestly 0.0. `/cost` / `/ping` must keep labelling
  spend "unknown" until agents actually report real costs (Gemini /
  OpenRouter / Agnes / FAL / Polly per-run spend). Do not re-introduce
  the old $0.09 / $0.38 figures.

### 13. Two `run_pipeline` entry points, different contracts
- `orchestration/graph.py::run_pipeline()` vs `pipeline.py::run_pipeline()`.
  Unify (or delete one) before adding new callers — new code must not
  pick the wrong one by accident.

### 14. Telegram buttons still partially unimplemented (by design, honest)
- `edt_` / `thm_` / `shorts_` reply "❌ Not implemented — no action
  taken". Wiring them needs a real pending-edit store + reply handler,
  a thumbnail-upload path, and a Shorts renderer. Do not claim they
  work until those exist. `/script` full script text cannot be shown —
  no script text is stored in `yt_videos`; a real script store is
  needed for that.

### 15. Ephemeral storage / LangGraph / self-healing — claims vs reality
- README's 4 cleanup layers are not wired to the pipeline; no S3
  lifecycle code exists. `pipeline.py::run_stage()` self-healing
  (`SWITCH_PROVIDER` / `SWITCH_KEY` / `RE_PROMPT`) is dead code on the
  production path. The "LangGraph" claim is wrong — no langgraph import
  exists; it is a plain sequential graph. Correct the README claims or
  implement the features; do not leave false architecture claims.

## P2 — Hygiene

- **Secrets masking** reveals first 6 + last 4 chars; PBKDF2 salt is
  static. Tighten masking and per-secret salt when rotating secrets.
- **SEC User-Agent** is a placeholder contact — set a real contact.
- **Test count mismatch:** README said 60, PROGRESS.md 62, audit
  counted 70 test methods. Keep the count honest in the new README or
  drop the number.
- **`requirements.txt`** has unused deps (langgraph, langchain-core,
  python-telegram-bot, tenacity, faster-whisper, …). Prune after the
  merge, verifying each removal against imports.
- **`.env.example` on main** showed `<redacted>` values in one read —
  likely tool-side redaction, **not confirmed**. Check the raw file in
  the GitHub UI; if real secrets are there, rotate immediately and
  purge from history.
- **Gemini native image path** (`_generate_gemini_native`) is
  `NotImplementedError` in PR #1 — Agnes now covers the image need, so
  either delete that path or implement it against the documented Gemini
  API. Do not leave a dead `NotImplementedError` as the "Gemini" image
  option in docs.

## Video editing + hooks — plan (not a promise)

Goal from the user (25 Nov 2026): clips where needed, editing hooks,
aimed at fast subscriber growth. This is a **plan**, not a guarantee.

**No promise is made of 1M subscribers in a month.** Growth depends on
content quality, retention, niche, consistency and distribution. A
pipeline can improve the *inputs* (hooks, pacing, captions); it cannot
guarantee the *outcome*.

### What to build (after P0 is done)

1. **Hook in the first 3 seconds (long-form) / first 1 second (Shorts).**
   Script prompt must require: a concrete, surprising, bundle-grounded
   claim or question in scene 1 — never a generic intro ("In this video
   we will…"). Verify step should flag a weak opening as a major issue.
2. **Agnes clips (5–10 s, `agnes-video-2.5-flash`) as B-roll / visual
   hooks** — one clip per key scene change, not a clip on every scene
   (cost/latency + async poll time). Clips are AI-generated visuals;
   they are **not real motion of a presenter** and do not fix the
   real-motion limitation: this account cannot generate real-motion
   presenter video (region-blocked, verified 24 Nov 2026 for a
   different project), so do not promise "real video" from Agnes clips.
3. **Captions (burned-in subtitles)** — currently an unimplemented
   claim in `assemble.py` (gap #11). Implement with the real transcript
   (Polly timings / script scenes), word-level if possible; test that
   captions do not cover faces / key chart labels.
4. **Pacing:** cut on every idea change; no static image longer than
   ~4–6 s without a zoom/pan or a chart/clip change; Shorts cut every
   1–2 s. Measure: render a Short, watch it back, count dead frames.
5. **Shorts pipeline:** graph now renders all 3 Shorts (PR #1 P1 fix)
   — verify all 3 actually render and are uploaded private, not just
   the first. Each Short needs its own hook + one idea + CTA, generated
   by the LLM from the bundle (no template padding).
6. **Retention feedback loop (needs real data):** once videos are
   public, read YouTube Analytics retention (average view duration,
   drop-off points) and feed the worst drop-off timestamp back into the
   script prompt for the next topic. No analytics wiring exists yet —
   this is future work, not part of this setup.
7. **Thumbnails:** Agnes image (`9:16` for Shorts, `16:9` for
   long-form) with a real, bundle-grounded claim — never a fabricated
   number on the thumbnail. Thumbnail upload path via the Telegram
   `thm_` button is still unimplemented (gap #14).

### What NOT to do

- Do not use Agnes clips to fabricate events, quotes or numbers.
- Do not add a hook that the script body does not deliver (clickbait
  that retention data will punish).
- Do not claim a Short is "viral-ready" — no such check exists.

## Next steps (PR #1 already merged — 25 Nov 2026; this PR is step 3)

**PR #1 merged: commit `8212f7623934226559b18c3b2e1b67a17552f5d5`, 35 files,
CI Tests run #18 PASSED.** The steps below start from merged `main`.
Do not push anything from this folder until you have pulled the merged
`main` and confirmed the Agnes patch still applies cleanly on top of it.

1. Confirm on `main`: PR #1 merged (`8212f762`), PR #2 merged
   (`d71e15e8`), CI green, `agent_chat.py` INTENT 2 still unpatched —
   that is a known next fix, not a regression. OAuth2 is implemented
   on branch `feat/youtube-oauth2` (PR #3, unmerged at the time this
   was written) — confirm it merges and CI stays green.
2. Verify the three YT Codespaces secrets exist (fresh GitHub sudo code
   will be needed — the 12:13 IST code has expired). Re-issue the
   refresh token if it is past ~2 Dec 2026.
3. This PR (`feat/agnes-media`, from updated `main`):
   - Adds `tools/agnes_ai.py` (new Agnes client).
   - Applies the Agnes wiring to the merged `tools/image_gen.py`
     (diffed first — the patch was exactly the merged file + wiring).
   - Merges the `.env.example` Agnes vars into the merged
     `.env.example` (other entries untouched).
   - Replaces the repo README with the honest-status version.
   - Adds `docs/SETUP.md` + `docs/GAPS.md`.
4. Verify the Agnes API details in gap #3 against the live docs, fix
   `agnes_ai.py` if the docs differ, then run SETUP.md §9 steps 4–5
   (real image + real video clip, usage page check).
5. (Done as PR #3, `feat/youtube-oauth2` — OAuth2 was kept on its own
   branch, not bundled into the Agnes PR.)
6. After OAuth2 merges: one full end-to-end run, private upload only,
   observed succeeding; and one failing run (e.g. bad topic) observed
   failing loudly. Only then discuss enabling the daily cron.
