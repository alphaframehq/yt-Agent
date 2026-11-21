# TubePilot AI 🎬🤖

> **Autonomous Research, Production, Publishing & Analytics Agent for Tech-Finance YouTube Content**

TubePilot AI is an enterprise-grade autonomous pipeline that designs, scripts, audits, renders, and publishes institutional-quality video content: **1 long-form breakdown (8–12 min)** and **3 daily Shorts (max 60s, 9:16 vertical)** every single day.

---

## 🏛️ System Architecture

```text
                                 [ Telegram Bot ]
                       (Keys, /run, Approval Buttons, Health)
                                       │
                                       ▼
 ┌─────────────────────────────────────────────────────────────────────────────┐
 │                           LangGraph State Machine                          │
 │                                                                             │
 │  ┌───────────────┐     ┌───────────────┐     ┌───────────────────────────┐  │
 │  │ ResearchAgent │ ──> │  ScriptAgent  │ ──> │     VerificationAgent     │  │
 │  └───────────────┘     └───────────────┘     │                           │  │
 │   • yfinance/EDGAR      • 8-12 min Longform  │ 1. 7 Deterministic (<1ms) │  │
 │   • TinyFish Crawl      • 3 Vertical Shorts  │ 2. 2 LLM Semantic Audits  │  │
 │   • Claim IDs (P&L)     • 300+ word SEO      └─────────────┬─────────────┘  │
 │                                                            │                │
 │                         Fix Loop (max 3 retries) ◄─────────┤ Fail           │
 │                                                            │                │
 │                                                            ▼ Pass           │
 │                                              ┌───────────────────────────┐  │
 │                                              │       Media Pipeline      │  │
 │                                              │                           │  │
 │                                              │ • 8 Institutional Charts  │  │
 │                                              │ • Polly Neural TTS        │  │
 │                                              │ • Visual Budget Tracker   │  │
 │                                              │ • MoviePy Composition     │  │
 │                                              └─────────────┬─────────────┘  │
 │                                                            │                │
 │                                                            ▼                │
 │                                              ┌───────────────────────────┐  │
 │                                              │       UploaderAgent       │  │
 │                                              │                           │  │
 │                                              │ • Private Upload YouTube  │  │
 │                                              │ • DynamoDB Registration   │  │
 │                                              │ • Post-Upload Disk Purge  │  │
 │                                              │ • Telegram Review Alert   │  │
 │                                              └───────────────────────────┘  │
 └─────────────────────────────────────────────────────────────────────────────┘
                                       │
                                       ▼
                       [ YouTube: Private Preview Link ]
                          [ 5 Telegram Action Buttons ]
```

---

## ⚖️ Content Pillars (70 / 30 Rule)

- **70% Finance to Technical:** How financial models, revenue engines, interchange fees, clearing networks, or regulatory moats dictate underlying engineering architectures (e.g. *How Stripe's Ledger Prevents Double-Spend*, *Why Revolut's Margin Depends on European Interchange Caps*).
- **30% Technical to Financial:** How system architectures, latency optimizations, protocols, and consensus mechanisms create business moats and enterprise valuations (e.g. *Solana Pay Technical Architecture vs. Visa TPS Unit Economics*).

---

## 🔒 Ephemeral Storage Guarantee

1. **YouTube IS Permanent Storage:** All local media files (MP4, MP3, PNG charts, temporary renders) are deleted immediately upon verified YouTube private upload.
2. **4 Cleanup Layers:**
   - Layer 1: Python `atexit` hook cleans local `/cache/renders` and temporary clips.
   - Layer 2: S3 24-hour lifecycle expiration rule.
   - Layer 3: DynamoDB TTL automatically cleans execution records (e.g. 7 days for verification logs, 30 days for research cache, 90 days for pipeline telemetry).
   - Layer 4: Defensive startup sweep sweeps any residual files from crashed runs.

---

## ⚙️ Prerequisites & Setup

### 1. Telegram Bot (Control Plane)
1. Message `@BotFather` on Telegram and create a new bot. Copy the **Bot Token**.
2. Find your personal Telegram user ID (using `@userinfobot`).
3. Set your environment variables:
   ```bash
   TELEGRAM_BOT_TOKEN="123456789:ABCdefGHIjklMNOpqrSTUvwxYZ"
   TELEGRAM_ADMIN_ID="987654321"
   ```

### 2. AWS Setup (Bedrock, DynamoDB & Polly)
1. **Region:** `us-east-1` (recommended for Bedrock model availability).
2. **Bedrock Model Access:** In the AWS Bedrock Console, request model access for:
   - `anthropic.claude-3-5-sonnet-20241022-v2:0` (Script generation)
   - `anthropic.claude-3-haiku-20240307-v1:0` (Fast research & verification)
3. **IAM Minimal Policy:** Ensure the IAM user has permissions for:
   - `dynamodb:*` (restricted to tables prefix `yt_*`)
   - `bedrock:InvokeModel`
   - `polly:SynthesizeSpeech`
4. Set credentials:
   ```bash
   AWS_ACCESS_KEY_ID="AKIA..."
   AWS_SECRET_ACCESS_KEY="..."
   AWS_DEFAULT_REGION="us-east-1"
   ```

### 3. DynamoDB Table Initialization
Run the provisioned table creator:
```bash
python -c "from db.setup_dynamo import setup_all_tables; setup_all_tables()"
```
This sets up 7 tables with **Provisioned 5 RCU / 5 WCU** and TTL attributes:
- `yt_videos`
- `yt_research_cache`
- `yt_verification_log`
- `yt_pipeline_runs`
- `yt_config`
- `yt_bot_requests`
- `yt_topics_queue`

### 4. Google Cloud Console (YouTube Data API v3)
1. In Google Cloud Console, enable **YouTube Data API v3**.
2. Create OAuth 2.0 Credentials (Desktop application).
3. Generate Refresh Token with scopes:
   - `https://www.googleapis.com/auth/youtube.upload`
   - `https://www.googleapis.com/auth/youtube`
4. Set credentials:
   ```bash
   YT_CLIENT_ID="..."
   YT_CLIENT_SECRET="..."
   YT_REFRESH_TOKEN="..."
   ```

### 5. AI API Keys
- `GEMINI_KEY`: Google AI Studio API key (used for Gemini 2.0 Flash fallback & verification).
- `OPENROUTER_KEY`: Optional fallback provider for DeepSeek / Llama 3 models.
- `PIXABAY_KEY`: Stock footage and music search.
- `FAL_KEY` / `REPLICATE_API_TOKEN`: AI visual generation fallback.

---

## 🤖 Telegram Bot Commands & Cheatsheet

| Command | Description | Example |
| :--- | :--- | :--- |
| `/ping` | Diagnostic health check (latency, API reachability, budget spend) | `/ping` |
| `/run [topic]` | Dispatch immediate pipeline run | `/run How Adyen Processes Payments` |
| `/status` | View status of current or last pipeline run | `/status` |
| `/pause` | Emergency kill switch — pauses automated runs | `/pause` |
| `/resume` | Unpause automated daily runs | `/resume` |
| `/keys list` | List all secrets (masked: `AIzaSy...6789`) | `/keys list` |
| `/keys set <KEY> <VAL>` | Hot-swap encrypted key without redeploy (auto-delete requested) | `/keys set GEMINI_KEY AIzaSy...` |
| `/model list` | Show active models for research, script, verify | `/model list` |
| `/model set <STAGE> <MODEL>` | Dynamically tier models | `/model set verify gemini/gemini-2.0-flash` |
| `/topic add <topic>` | Enqueue high-priority topic | `/topic add Plaid Financial API 2` |
| `/topic list` | List queued topics by priority | `/topic list` |
| `/config view` | Display runtime parameters (budgets, thresholds) | `/config view` |
| `/config set <KEY> <VAL>` | Update runtime config parameters | `/config set budget_ai_per_video 0.40` |
| `/codespace list` | List active GitHub Codespaces | `/codespace list` |
| `/codespace stop [name]` | Immediately stop Codespace to prevent idle charges | `/codespace stop fuzzy-robot-1234` |

---

## 🎛️ Interactive Telegram Approval Flow

When a video completes, the bot delivers a private YouTube link with 5 interactive buttons:

1. **🚀 Publish Public (`pub_<id>`):** Changes privacy status on YouTube from `private` to `public`.
2. **🗑️ Reject & Delete (`rej_<id>`):** Deletes video from YouTube and removes from DynamoDB.
3. **✏️ Re-Edit Script (`edit_<id>`):** Enqueues script for re-prompting with human notes.
4. **🔄 Re-Render (`render_<id>`):** Re-runs media composition with fresh visual seed.
5. **📱 Post Shorts (`shorts_<id>`):** Triggers upload and scheduling for the 3 daily Shorts.

---

## 🛡️ Self-Healing Error Taxonomy

The pipeline implements autonomous classification and recovery for 7 error classes:

| Error Class | Trigger | Autonomous Action |
| :--- | :--- | :--- |
| `TRANSIENT` | HTTP 429, 503, SocketTimeout | `retry_same` with exponential backoff (2s, 4s, 8s) |
| `PROVIDER_DOWN` | HTTP 502, 504, ConnectionRefused | `switch_provider` (Bedrock -> Gemini -> OpenRouter) |
| `INVALID_KEY` | HTTP 401, 403, AuthenticationFailed | `switch_key` from standby secret pool in DynamoDB |
| `QUOTA_EXCEEDED` | Daily quota exceeded | `reschedule_next_day` + Telegram alert |
| `SCHEMA_FAIL` | Pydantic validation error | `re_prompt` with validation error schema in context |
| `VERIFY_FAIL` | Hallucinated figures, no citations | `fix_loop` (max 3 deterministic rewrite iterations) |
| `FATAL` | Hardware failure, unrecoverable system fault | `abort` + immediate admin alert |

---

## 🧪 Running Tests

TubePilot AI includes 54 comprehensive unit, pipeline, and integration tests with zero external dependency requirements (all cloud APIs feature robust in-memory mock fallbacks):

```bash
# Run full 54-test suite
python -m unittest discover -s tests -p "test_*.py" -v
```

Test breakdown:
- `tests/test_tools.py`: 9 tests (LLM provider chain, image cache, SEC EDGAR, yfinance, Polly SSML, etc.)
- `tests/test_research.py`: 2 tests (ResearchBundle validation, citation extraction)
- `tests/test_verify.py`: 7 tests (7 deterministic checks: numbers, compliance, disclaimer, etc.)
- `tests/test_healer.py`: 6 tests (Autonomous error classification and recovery actions)
- `tests/test_graph.py`: 4 tests (LangGraph state machine, fix loops, human review escalation)
- `tests/test_media.py`: 6 tests (8 institutional charts, Polly TTS alignment, visual budget tracker, assembly)
- `tests/test_storage_uploader.py`: 7 tests (DynamoDB CRUD, TTL, ephemeral disk purge, uploader agent)
- `tests/test_bot.py`: 8 tests (Telegram commands, key hot-swapping, security masking, Codespace shutdown)
- `tests/test_integration.py`: 5 tests (End-to-end dev_run runner, notifications, Codespace hooks)

---

## 🚀 Running Locally or in GitHub Codespaces

```bash
# Execute single run with specific topic
python scripts/dev_run.py --topic "How Stripe Makes Money" --pillar finance_to_technical --no-stop

# Automated Codespace execution (automatically shuts down Codespace on finish)
python scripts/dev_run.py
```

---

## 📄 License
MIT License. Built autonomously for **TubePilot AI**.
