# yt-Agent TTS fix — runs #19 / #20 root cause (27 Nov 2026)

**Root cause (verified from the public run logs):** research, script and
verify all passed in runs #19 (22 min) and #20 (4 min). Both died in
finalize at Gemini TTS with **HTTP 429**. The key pool had only ONE key
(log: `key 1/1`), it went into a 60s cooldown, and there is no AWS Polly
fallback ("no AWS Polly keys exist"). Nothing was rendered or uploaded.
Run #20 also showed `gemini-2.5-flash-lite` returning **HTTP 404**
(retired model, still in the Shorts fallback chain) and Shorts
`_ShortsList` validation errors (`pillar` Field required, `seo` must be
a `SEOData` dict).

**Status: fixed in this clone only. NOT pushed, NO run triggered.**
Live proof still needs a real run (#21) after Dev adds the second key.

## What changed

### 1. TTS key pool — `tools/secrets.py` (already supported it)
`get_gemini_pool_keys()` already discovers `GEMINI_KEY`,
`GEMINI_KEY_POOL` and `GEMINI_KEY_1..20` — so `GEMINI_KEY_2` works
with no code change. What was missing was everything around it
(see 2). No key value is ever logged — only the key index (`1/N`)
and masked prefixes.

### 2. TTS Retry-After + retries + pacing — `tools/gemini_tts.py`
`synthesize_speech()`:
- **Retry-After honoured:** a 429/5xx response's `Retry-After` header
  is parsed (seconds or HTTP-date) and waited, capped at 60s
  (`_parse_retry_after_seconds`, `_RETRY_AFTER_MAX_WAIT_SEC`).
- **Up to 3 attempts** across the whole pool per scene
  (`_TTS_MAX_ATTEMPTS`); between rounds it sleeps the Retry-After
  value, else a 10s backoff. A 400 (bad request) or a safety refusal
  still raises immediately — retrying those is pointless.
- **Pacing:** successive real TTS API calls are spaced at least
  `TTS_MIN_INTERVAL_SEC` apart — **default 21s**, env-overridable,
  `0` disables (tests set `0`). Cache hits are never paced. This
  applies to every caller (long video scenes + Shorts scenes) because
  it lives in `synthesize_speech`, not in one agent.
- 500/502/503/504 now fail over to the next key like a 429 (was:
  raised immediately and burned the scene).

`agents/tts.py`: docstring only — pacing is documented as living in
`tools/gemini_tts.py`. No behaviour change there; Polly fallback is
unchanged (tried only when AWS keys exist — they don't, and none
were invented).

### 3. Retired model removed — `tools/llm_provider.py`, `config_manager.py`
- `FREE_TIER_TEXT_MODELS`: `"gemini-2.5-flash-lite"` removed.
- `DEFAULT_CONFIG["verify_chain"]`: the `["gemini",
  "gemini-2.5-flash-lite"]` rung removed.
- **Also update the Sheets `yt_config` tab** if it stores a
  `verify_chain` row: that row overrides this default, so the live
  run will keep calling the retired model until the row is fixed.
- Guard test: `tests/test_no_retired_models.py` fails if the model
  id reappears as live code (removal comments are allowed).

### 4. Shorts validation repair — `agents/script.py`, `tools/llm_provider.py`
- `tools/llm_provider.py`: `call_structured()` /
  `call_with_fallback()` accept `validation_context`, forwarded to
  `schema.model_validate(data, context=...)`.
- `agents/script.py`: `_ShortsList` gained a `mode="before"`
  validator that repairs LLM Shorts deterministically:
  missing/invalid `pillar` is filled from the bundle pillar passed
  in context (never invented; defaults to `ai_everything` only when
  no context is given); a missing or wrong-type `seo` is rebuilt
  from the Short's own narration (title ≤ 100 chars, description
  starting with the compliance disclaimer and padded with the
  Short's own narration to the 300-word SEO minimum, tags 15–25
  with the topic keyword first). A Short with no scenes is still
  left to fail validation loudly — scenes are never invented.
- `generate_shorts()` passes
  `validation_context={"pillar": bundle.pillar, "topic": bundle.topic}`.
- **Not changed:** the existing Gemini `3.8-flash` +
  `3.5-flash-lite` chain stays; no new provider or key was added.
  The Imagen 3 vision step was left alone (task scope).

### 5. Clone state — read before pushing
This clone was **behind `origin/main`** when the fix started
(`HEAD b5de7d4`, `origin/main d5d3afc`). Four files that were clean
in the working tree were synced to `origin/main` first, then fixed:
`tools/gemini_tts.py`, `tools/llm_provider.py`,
`config_manager.py`, `models/script.py`. The other uncommitted
changes in this clone were left untouched. **Do not push from this
mixed tree as-is** — rebase/merge onto `main` first, or the push
will be based on an old commit.

## What Dev must do himself (2 minutes, no new Google account)

The second key MUST be from a **different Google Cloud project**
than the first key — same Google account, new project — otherwise
both keys share one free-tier quota and the 429 comes back.

1. Go to **Google AI Studio** (`aistudio.google.com`), sign in as
   `alphaframehq@gmail.com`.
2. Top-left project dropdown → **New project** → create it (any name,
   e.g. `yt-agent-tts-2`). This is a project, not a new account.
3. **Get API key** → **Create API key in new project** → copy the key.
   Do NOT paste it in chat, in code, or in Google Sheets.
4. GitHub repo `alphaframehq/yt-Agent` → **Settings → Secrets and
   variables → Actions → New repository secret** → name
   **`GEMINI_KEY_2`** → paste the key → **Add secret**.
5. Re-trigger the workflow (Actions → TubePilot AI Daily Automation
   → Run workflow). That is run #21 — the first live proof.

## What still needs a live run to prove (run #21)
- TTS with two keys: key 1 429s → key 2 takes over, scenes complete.
- Pacing: 21s gaps between scene calls, no new 429 storm.
- Shorts: a Short with missing pillar/seo validates and renders.
- The retired model is gone from the live chain (also fix the
  Sheets `yt_config` verify_chain row if it still lists it).

## Tests (local, light venv `/tmp/yt-test-venv`)
Targeted: `tests/test_gemini_tts.py tests/test_gemini_pool.py
tests/test_shorts_repair.py tests/test_no_retired_models.py` —
**30/32 passed**. The 2 failures are pre-existing and unrelated:
`test_gemini_pool` tests 4 and 5 fail identically on pristine
`HEAD` (mock/`httpx` incompatibility when the code builds an
`HTTPStatusError` from a mocked request) — verified with a
`git worktree` of `HEAD`.

Full suite: **274/293 passed, 19 errors**, all pre-existing
environment gaps in this light venv (missing `googleapiclient`
→ publish-schedule/youtube-oauth tests, missing `matplotlib` →
assemble/media tests, missing `sec-edgar-api`, the 2 pool-mock
errors above). Zero new failures vs the `HEAD` baseline (15
errors there; the 5 extra here are the untracked
`test_publish_schedule.py`, which does not exist at `HEAD`).
Run the full suite in CI, where the full `requirements.txt` is
installed, before relying on it.
