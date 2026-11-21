# TubePilot AI — Design Decisions & Assumptions

## D-001: DynamoDB Provisioned Mode
Using PROVISIONED with 5 RCU/5 WCU per table to avoid pay-per-request surprises.

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

## D-008: Automated Codespace Shutdown
To prevent idle compute billing when running inside GitHub Codespaces, `scripts/dev_run.py` explicitly invokes `gh codespace stop` within a `finally` block unless `--no-stop` is passed. `.devcontainer/devcontainer.json` also configures `shutdownAction: stopContainer`.

