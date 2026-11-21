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
