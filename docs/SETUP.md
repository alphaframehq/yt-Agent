# TubePilot AI — Setup Guide (from scratch)

> **Scope:** setup guide, prepared 25 Nov 2026. No live account was
> changed by writing this file.
> **Precondition:** PR #1 (`fix/production-mocks`) is **merged into
> `main`** (25 Nov 2026, merge commit `8212f7623934226559b18c3b2e1b67a17552f5d5`,
> CI Tests run #18 PASSED). The setup below assumes the patched,
> fail-loud code on `main`. On unpatched `main`
> (c39f42541b8e784dcf41c09583dc57227be07667) the pipeline will happily
> fake success; do not set up production on it.
>
> **Golden rules**
> 1. `AWS_REGION=us-east-2` — everywhere, no exceptions.
> 2. LLM = Gemini (free) + OpenRouter. **NO Bedrock.**
> 3. Never paste a real key/token/secret into chat, a file, a log, or a
>    repo. Secret *names* only in this guide.
> 4. If a step fails, the pipeline must fail loudly. That is the intended
>    behaviour — do not "fix" it by re-adding a mock.

## 0. What you need before you start

- A Google account that owns the YouTube channel uploads go to
  (here: `alphaframehq@gmail.com`, channel `UCs3Pxla45RH-V0-JcHxp42A` —
  already verified 25 Nov 2026).
- A Telegram bot token + your Telegram user ID (admin).
- An AWS account with DynamoDB + Polly access in `us-east-2`.
- Gemini AI Studio key (free) and an OpenRouter key.
- An Agnes AI account (Google sign-in works) — key named `mus` already
  exists, created 25 Nov 2026.
- GitHub account `alphaframehq` with access to `alphaframehq/yt-Agent`.

## 1. GCP — YouTube Data API v3 + OAuth (already done 25 Nov 2026 — verify, don't redo)

What was already created 25 Nov 2026 (browser-verified, 37 min):

| Item | Value |
|---|---|
| GCP project | **TubePilot AI** — ID `tubepilot-ai-515805` |
| API | YouTube Data API v3 — **enabled**, verified live via `channels.list mine=true` |
| Channel | `UCs3Pxla45RH-V0-JcHxp42A` |
| OAuth consent | External, **Publishing status: Testing**, app "TubePilot AI", support email `alphaframehq@gmail.com`, test user `alphaframehq@gmail.com` |
| Scopes | `https://www.googleapis.com/auth/youtube.upload`, `https://www.googleapis.com/auth/youtube` |
| OAuth client | **"TubePilot Desktop"** (Desktop app), Client ID `79629915270-a4r00uu60pm6l65gc7pi36gnf5sc5v5h.apps.googleusercontent.com` |
| Refresh token | Obtained 25 Nov 2026 (127-char), saved in Cloud Shell `~/tubepilot-token.json` (project `tubepilot-ai-515805`) — **stored + verified 25 Nov 2026** as a GitHub Codespaces secret and in the Codespace `.env` (see §7) |

### 1a. ⚠️ 7-day Testing-mode token warning — read this first

**A refresh token from an app in Testing publishing status expires
after 7 days.** The token obtained 25 Nov 2026 therefore expires
around **2 Dec 2026** unless one of these happens:

- **Option A (recommended for production): publish the OAuth app.**
  GCP Console → APIs & Services → OAuth consent screen → Publish app.
  Publishing triggers Google verification, which can take days and may
  ask for a privacy policy / YouTube API compliance review. Do not start
  this the day you need uploads.
- **Option B (stopgap): re-issue the refresh token weekly.** Repeat the
  OAuth consent flow (OAuth Playground or the bot's auth flow once
  implemented), store the new `YT_REFRESH_TOKEN`, and verify with
  `channels.list`. Set a weekly reminder — a silently expired token
  means uploads fail loudly at 04:00 with no video that day.

If you create a new client secret ("Add secret" in GCP — rotation path,
old secret stays enabled), the **old** secret remains valid; store the
new one and verify before deleting anything.

### 1b. If you need to redo / verify the GCP setup

1. https://console.cloud.google.com → project **TubePilot AI**
   (`tubepilot-ai-515805`).
2. APIs & Services → Library → search "YouTube Data API v3" → Enable.
3. APIs & Services → OAuth consent screen: External, app name
   "TubePilot AI", support email, scopes `youtube.upload` + `youtube`,
   add your Gmail as a test user. **Do not publish yet unless you are
   ready for verification (see 1a).**
4. APIs & Services → Credentials → Create Credentials → OAuth client ID
   → Desktop app → name "TubePilot Desktop". Note the Client ID and
   download the client-secret JSON **once** — Google no longer lets you
   re-view/download it later; the secret is shown only masked.
5. Generate a refresh token with those credentials (OAuth Playground,
   `https://developers.google.com/oauthplayground`) with the two scopes
   above. The repo has no token-*generation* flow — it only *uses* a
   stored refresh token (PR #3). Verify it works:
   `channels.list mine=true` must return your channel.
6. Store the three values as GitHub Codespaces secrets (see §7) —
   **never** in a file in the repo, and never in chat.

### 1c. OAuth2 in code — IMPLEMENTED (PR #3, 25 Nov 2026), live test still required

Real OAuth2 is now implemented in `tools/youtube_api.py::
_get_youtube_service()` — it builds
`google.oauth2.credentials.Credentials` from `YT_CLIENT_ID` +
`YT_CLIENT_SECRET` + `YT_REFRESH_TOKEN` (env only), refreshes the
access token as needed, and passes `credentials=...` (never
`developerKey=...`) to `build("youtube", "v3", ...)`. An API key is
read-only and can never authorise writes. Missing env vars or a
rejected refresh token (invalid_grant — expired/revoked) raise
`RuntimeError` loudly; uploads return only the real video ID from the
API, with the title taken from the script bundle (no invented default).

**This does NOT yet mean uploads are proven.** A real private-upload
end-to-end test is still REQUIRED before enabling the daily cron
(§9 step 6): one successful private upload observed, plus one failing
run observed failing loudly. See `docs/GAPS.md` #1. No video was
uploaded by PR #3 itself.

## 2. LLM — Gemini (free, primary) + OpenRouter (fallback). NO Bedrock.

1. **Gemini AI Studio** (https://aistudio.google.com): create an API key
   (free tier). This is the primary LLM for research, script and verify.
   For pool failover you may add up to 20 keys:
   `GEMINI_KEY` (primary), `GEMINI_KEY_POOL` (comma-separated or JSON
   list), or `GEMINI_KEY_1` … `GEMINI_KEY_20`.
   The patched `tools/llm_provider.py` raises if no Gemini key is set —
   it no longer returns `"{}"` and no longer blocks the OpenRouter
   fallback.
2. **OpenRouter** (https://openrouter.ai): create a key. Fallback when
   Gemini fails / rate-limits. `OPENROUTER_KEY`.
3. **Bedrock: do not provision.** The old README required Bedrock, the
   old `.env.example` said "Bedrock skipped", and config used Gemini.
   User decision 25 Nov 2026: **Gemini + OpenRouter only.** If you see
   a Bedrock model name anywhere in config (`claude-3-5-haiku` was a
   default in `db/dynamo.py`, now patched to `"unknown"`), treat it as a
   stale default, not a requirement.

Verify: run the LLM path with no key set — it must **raise**, not return
an empty script. Then set the key and confirm a real structured call
succeeds (see §9 smoke test).

## 3. AWS — DynamoDB (us-east-2) + Polly

1. Region: **`AWS_REGION=us-east-2`** in every env, workflow file and
   script. Do not use `us-east-1` (old `setup_dynamo.py`) or
   `ap-southeast-2` (old `.env.example`).
2. Create an IAM user/role with `dynamodb:*` on the 7 `yt_*` tables and
   `polly:SynthesizeSpeech`. Store `AWS_ACCESS_KEY_ID` /
   `AWS_SECRET_ACCESS_KEY` as Codespaces secrets.
3. Provision the tables (patched `db/setup_dynamo.py` now raises if the
   region is missing or a table is missing after provisioning, and
   verifies all 7 tables exist):
   ```bash
   export AWS_REGION=us-east-2
   python -m db.setup_dynamo
   ```
   Tables (7, `yt_*` prefix — confirm exact names in `db/setup_dynamo.py`
   before running; the audit records 7 tables, names were not re-listed
   here): videos, verification logs, topics queue, config (`yt_config`),
   secrets, and two further `yt_*` tables. If `_verify_tables_exist()`
   raises, do not proceed — the runtime will look in the wrong region
   otherwise.
4. Polly: no extra setup beyond the IAM permission. The patched
   `tools/polly.py` raises on any Polly failure — a silent 1-sec MP3 is
   no longer produced. `ALLOW_DEMO_MODE` must stay off in production.

## 4. Telegram bot

1. Talk to `@BotFather` → `/newbot` → copy `TELEGRAM_BOT_TOKEN`.
2. Get your numeric Telegram user ID (e.g. via `@userinfobot`) — this is
   `TELEGRAM_ADMIN_ID`. **Mandatory:** if unset, the patched bot denies
   everyone (fail-closed) and logs a security warning. There is no
   "open to all" mode.
3. Store both as Codespaces secrets (see §7).
4. The bot runs via `.github/workflows/bot.yml` — cron every 4h,
   `python -u telegram_bot/bot.py --run-duration 12600`, self-retrigger
   chain. Missing/invalid token now exits `1` (was silent exit 0).
5. Known gap: `telegram_bot/agent_chat.py` INTENT 2 still replies
   "Production Run Dispatched!" without checking dispatch — fix before
   trusting agent-chat runs (docs/GAPS.md #2).

## 5. Agnes AI — free image + video (new, 25 Nov 2026)

Verified 25 Nov 2026 by signed-in browser inspection (Google sign-in
with `alphaframehq@gmail.com`):

| Item | Verified value |
|---|---|
| Platform | Agnes AI (SapiensAI), OpenAI-compatible API |
| Base URL | `https://apihub.agnes-ai.com/v1` |
| Auth | `Authorization: Bearer <AGNES_API_KEY>` |
| Existing key | Named **`mus`**, created 2026-11-25 06:37:50 UTC, last used `---` (never used) |
| Balance | **$0.00** current, $0.00 voucher; **no Token Plan subscribed**; usage all zero (0 tokens, 0 images, 0 video seconds, 0 requests) |
| Docs | https://www.agnes-ai.com/en/docs/overview |
| Image docs | https://www.agnes-ai.com/en/docs/agnes-image-25-flash |
| Video docs | https://www.agnes-ai.com/en/docs/agnes-video-25 and `.../agnes-video-25-flash` |

### 5a. Free tier — what is actually free, and the caveat

There is **no free-credit balance**. Free access is an
**"Angel User Trial Price" of $0** in the docs, for the Flash models
only:

| Model | What it does | Trial price | List price (if trial ends) |
|---|---|---|---|
| `agnes-image-2.5-flash` | Text-to-image + image editing (up to 16 input images, <20 MB each), 1K/2K/4K, ratios 1:1, 2:3, 3:2, 3:4, 4:3, 9:16, 16:9, 21:9, PNG out | **$0 / image, all sizes** | $0.03 / $0.06 / $0.12 per image (1K/2K/4K) |
| `agnes-video-2.5-flash` | Text-to-video, first/last-frame control, image/audio/video refs, 720P/1080P/1K/2K, **5–10 sec** | **$0 / sec, all sizes** | $0.006 / $0.011 / $0.016 per sec (720P / 1080P+1K / 2K) |
| `agnes-video-2.5` (non-Flash) | 4–12 sec, same sizes | **Not free** | $0.025 / $0.040 / $0.055 per sec |

> ⚠️ **Trial expiry/limit was NOT stated on any page seen.** Re-check
> the docs / billing page before relying on $0 — if the trial ends,
> calls will either fail or start billing against a $0.00 balance
> (i.e. fail). Treat $0 as "currently $0, unverified duration", never
> as a permanent free tier. The code in `tools/agnes_ai.py`
> raises loudly on a non-zero / payment-required response instead of
> silently continuing.

### 5b. How the API works (what the code in `tools/agnes_ai.py` implements)

- **Image (synchronous):** `POST {AGNES_BASE_URL}/images/generations`
  with `{"model": "agnes-image-2.5-flash", "prompt": ..., "size": ...}`.
  Response is OpenAI-compatible (`data[0].b64_json` or `data[0].url`).
  The patch downloads the URL / decodes base64, validates non-empty
  PNG bytes, and writes the file. Any HTTP error raises — no Pillow
  placeholder.
- **Video (asynchronous — create, then poll):**
  `POST {AGNES_BASE_URL}/videos` → returns a video/task ID, then poll
  `GET {base-without-/v1}/agnesapi?video_id=<ID>&model_name=agnes-video-2.5-flash`
  until `completed` / `failed`. Default poll timeout 600 s, interval
  10 s. On `failed` or timeout the patch raises `RuntimeError` — no
  empty MP4, no fake clip. On success it downloads the video URL and
  validates the file is non-empty.
  > The poll path (`/agnesapi?...`) and the exact create-payload fields
  > are taken from the Agnes docs pages listed above as reported by the
  > browser inspection; the **exact JSON field names for `size` /
  > `duration` on the create call were not re-verified character-for-
  > character in this session** — verify them against
  > https://www.agnes-ai.com/en/docs/agnes-video-25-flash before the
  > first live call, and fix `tools/agnes_ai.py` if the docs
  > differ. Do not guess and ship.
- **Env vars:** `AGNES_API_KEY`, `AGNES_BASE_URL`
  (default `https://apihub.agnes-ai.com/v1` if unset — set it explicitly
  in Codespaces secrets anyway, so a base-URL change is visible).

### 5c. Storing the Agnes key

The key named `mus` already exists in the Agnes account. Its **value**
was never copied or shown in any report — do not ask for it in chat.
Store it as the Codespaces secret `AGNES_API_KEY` (and
`AGNES_BASE_URL=https://apihub.agnes-ai.com/v1`) via the browser /
GitHub UI, straight from the Agnes API-keys page
(https://platform.agnes-ai.com/settings/apiKeys), without pasting the
value anywhere else. The client reads it via `tools/secrets.get_secret("AGNES_API_KEY")`
(DynamoDB store first, env fallback) — same resolution order as the
other secrets.

### 5d. Where Agnes fits in the pipeline

- **Images:** `tools/image_gen.py` — pass
  `model="agnes/agnes-image-2.5-flash"`, or set
  `AGNES_AS_DEFAULT_IMAGE_PROVIDER=1` with `AGNES_API_KEY` set.
  Imagen 3 / FLUX remain as fallbacks when Agnes is not configured.
  Never the Pillow placeholder on the success path.
- **Video clips:** call `tools/agnes_ai.py:generate_video_clip()`
  directly from `agents/visuals.py` / `agents/assemble.py` for 5–10 s
  B-roll / hook clips (see `docs/GAPS.md` § "Video editing + hooks").
  Agnes clips are **short clips, not real motion of a presenter** —
  they do not remove the real-motion limitation noted in `docs/GAPS.md`.
- **What Agnes does NOT replace:** Gemini Imagen 3 (if you prefer it),
  FAL video (`FAL_KEY`, longer generations), Pixabay stock
  (`PIXABAY_KEY` — music still hardcoded, see docs/GAPS.md).

## 6. Other services

| Service | Key / secret | Notes |
|---|---|---|
| Research (TinyFish) | `TINYFISH_API_KEY` | Patched code raises if missing/invalid; zero articles raises |
| Stock media | `PIXABAY_KEY` | Music search still returns one hardcoded track (P1) |
| Video (FAL) | `FAL_KEY` | Unchanged by PR #1 |
| Images (Replicate FLUX) | `REPLICATE_API_TOKEN` | Fallback path in `image_gen.py` |
| GitHub dispatch | `GH_PAT` | `/run` honestly reports dispatch failure without it; needs `repo` + workflow scope to push workflow files (see docs/GAPS.md) |
| Secrets encryption | `MASTER_SECRET` | Fernet key for DynamoDB `yt_config` secrets; decrypt failure now raises |

## 7. Secrets table — GitHub Codespaces secrets (exact names)

Store these in the repo's Codespaces secrets
(Repo → Settings → Secrets and variables → Codespaces), **not** in
`.env` files committed to the repo. Values are never written in this
guide.

| Secret name | Source | Required for |
|---|---|---|
| `YT_CLIENT_ID` | GCP OAuth client "TubePilot Desktop" | YouTube OAuth2 (once implemented) |
| `YT_CLIENT_SECRET` | GCP OAuth client (download once; Google will not re-show it) | YouTube OAuth2 |
| `YT_REFRESH_TOKEN` | OAuth consent flow, 25 Nov 2026 (Cloud Shell `~/tubepilot-token.json`) | YouTube OAuth2 — **expires ~2 Dec 2026 in Testing mode** |
| `GEMINI_KEY` | Gemini AI Studio | LLM primary |
| `GEMINI_KEY_POOL` | Gemini AI Studio (comma/JSON list) | LLM pool failover (optional) |
| `OPENROUTER_KEY` | OpenRouter | LLM fallback |
| `AGNES_API_KEY` | Agnes AI, key named `mus` | Agnes image + video |
| `AGNES_BASE_URL` | `https://apihub.agnes-ai.com/v1` | Agnes image + video |
| `TELEGRAM_BOT_TOKEN` | @BotFather | Telegram bot |
| `TELEGRAM_ADMIN_ID` | Your numeric Telegram user ID | Telegram auth (fail-closed) |
| `AWS_ACCESS_KEY_ID` | AWS IAM | DynamoDB + Polly |
| `AWS_SECRET_ACCESS_KEY` | AWS IAM | DynamoDB + Polly |
| `AWS_REGION` | `us-east-2` (literal value, not a secret — set everywhere) | DynamoDB region |
| `MASTER_SECRET` | Generate locally, store once | Fernet encryption of DynamoDB secrets |
| `GH_PAT` | GitHub personal access token | Actions dispatch from Telegram |
| `PIXABAY_KEY` | Pixabay | Stock media |
| `TINYFISH_API_KEY` | TinyFish | Research |
| `FAL_KEY` | FAL | Video generation |
| `REPLICATE_API_TOKEN` | Replicate | FLUX images |

Also set in the Codespace `.env` (from `.env.example` in the repo root):
`GH_REPO_OWNER=alphaframehq`, `GH_REPO_NAME=yt-Agent`.

> **Token-storage status, 25 Nov 2026 (verified):** `YT_CLIENT_ID` /
> `YT_CLIENT_SECRET` / `YT_REFRESH_TOKEN` are stored as GitHub
> Codespaces secrets (visible to yt-Agent only) and in the Codespace
> `.env` (git-ignored). The first refresh-token entry had 3 characters
> misread (zeros instead of capital O's) and failed `invalid_grant`;
> it was corrected 12:21 IST via a GitHub email code. Verified three
> ways with `channels.list mine=true` (Cloud Shell token + new secret,
> Codespace `.env`, and after a Codespace restart using only the
> GitHub secrets) — all returned channel Alpha Frame. A **new client
> secret** was created in GCP because Google no longer lets you
> view/download an existing one; the old secret stays enabled.

## 8. Running it

```bash
# 1. Codespace / runner, repo at main AFTER PR #1 is merged
# 2. Copy the corrected template and fill it (or use Codespaces secrets)
cp .env.example .env        # .env is NEVER committed
# 3. Provision DynamoDB (us-east-2)
export AWS_REGION=us-east-2
python -m db.setup_dynamo
# 4. Smoke test (see §9) — do this before enabling the cron
# 5. Bot (GitHub Actions bot.yml does this on a 4h cron):
python -u telegram_bot/bot.py --run-duration 12600
# 6. Daily pipeline (daily.yml, 04:00 UTC):
python scripts/dev_run.py --scheduled
```

**Deployment warning:** there is no `main.py` / Dockerfile. The bot
depends on the GitHub Actions retrigger chain and on a Codespace staying
alive for local runs. Decide the 24/7 host before calling this
production (docs/GAPS.md #4). A Codespace stopping stops the bot.

## 9. Smoke test — before you enable the cron

Run these in order. **Every failure must raise / exit non-zero — a
silent "success" is a bug, not a pass.**

1. **No-key fail-loud:** unset `GEMINI_KEY` / `OPENROUTER_KEY`, run a
   script-generation call — it must raise, not return a template.
2. **LLM:** with keys set, confirm a real structured script call returns
   bundle-grounded scenes with real `claim_id` citations.
3. **Research:** an unknown/private topic (e.g. "Stripe") must raise
   asking for an explicit public ticker — never silently use `SQ`.
4. **Agnes image:** `generate_image(prompt, model="agnes/agnes-image-2.5-flash")`
   must return a real non-empty PNG. Then check the Agnes usage page —
   the call should appear (0 images before, 1 after).
5. **Agnes video:** `generate_video_clip(prompt, duration_sec=5)`
   must return a real non-empty MP4 after create-then-poll. Confirm the
   poll loop, not a stub.
6. **YouTube (after OAuth2 is implemented):** upload a test video as
   **private**, confirm `verify_video_exists()` returns True, confirm
   the DynamoDB record, confirm local media is purged only after that.
   Never test with a public upload.
7. **Telegram:** `/ping` with no videos must say "No videos produced
   yet" (not a fake Stripe video); `/run` with `GH_PAT` unset must say
   "Dispatch failed", not "Dispatched!".
8. **Empty queue:** `scripts/dev_run.py --scheduled` with an empty queue
   must log `queue empty, nothing to run` and exit 0.

If any step returns fake success, stop — the fail-loud patches are not
applied correctly.
