# Daily Workflow — web-UI apply steps (1 long video + 3 Shorts/day)

## Why this file exists

Workflow files (`.github/workflows/*.yml`) **cannot be pushed via the
GitHub API** — the API returns 403 for them. The updated workflow
below lives in this clone only, until a human applies it in the
GitHub web UI. Nothing else in this change needs the web UI: all
Python code (`tools/trends.py`, `agents/shorts.py`,
`scripts/daily_run.py`, Sheets tabs) ships through a normal PR.

## What changed in `.github/workflows/daily.yml`

Compared with the current `main` version:

1. **Runner command** — the pipeline step now runs the new daily
   runner instead of the single-video runner:
   - Before: `python -u scripts/dev_run.py --pillar finance_to_technical --no-stop`
   - After: `python -u scripts/daily_run.py --pillar ai_everything --shorts-count 3 --scheduled --no-stop`
   - The niche is now AI (`ai_everything` — anything new in AI:
     tools, projects, discoveries, inventions, AI scams, predictions,
     strategy, formulas, ML/LLM training, pros/cons, leaks,
     conspiracy). The `ai_everything` pillar itself (dev_run.py /
     pipeline.py defaults + `prompts/script_ai_everything.txt`) is
     added by a separate change — merge that first, or the workflow
     run will fail on an unknown pillar.
   - `--scheduled` makes an empty Sheets queue exit cleanly ("queue
     empty, nothing to run") instead of erroring; `--shorts-count 3`
     is the daily default (0 = long video only).
2. **New `workflow_dispatch` input `shorts_count`** (choice `0/1/2/3`,
   default `3`) so a manual run can pick how many Shorts to make.
   The existing `pillar` input now offers `ai_everything` first
   (default `ai_everything`).
3. **Success notify text** now says "1 long video + 3 Shorts" and
   names the three Sheet tabs (`yt_videos`, `yt_shorts`,
   `yt_trends`).
4. **Nothing else changed**: same schedule (`0 4 * * *` = 04:00 UTC
   daily), same secrets/env, same `nick-fields/retry@v3` wrapper
   (40 min timeout, 2 attempts), same 60-min job timeout, same
   Telegram failure notify. `dashboard.yml` and
   `scripts/export_dashboard_data.py` are untouched.

## How to apply (github.com, ~5 minutes)

You must be signed in to GitHub as a user with write access to
`alphaframehq/yt-Agent`. Do this **after** the code PR containing
`scripts/daily_run.py`, `agents/shorts.py`, `tools/trends.py` and
the Sheets changes is merged — the workflow calls `daily_run.py`,
which must exist on `main` first — **and after the separate
`ai_everything` pillar change is merged** (dev_run.py/pipeline.py
defaults + `prompts/script_ai_everything.txt`), since the workflow
runs with `--pillar ai_everything`.

1. Open
   `https://github.com/alphaframehq/yt-Agent/blob/main/.github/workflows/daily.yml`
2. Click the **pencil (Edit this file)** icon, top-right above the
   file contents.
3. Replace the **entire file contents** with the contents of
   `.github/workflows/daily.yml` from this clone (copy-paste the
   whole file; do not edit it piecemeal — the `shorts_count` input,
   the `daily_run.py` command line and the notify text must all land
   together).
4. Scroll down to **Commit changes**:
   - Commit message: `feat: daily 1 long video + 3 Shorts via daily_run.py`
   - Leave "Commit directly to the `main` branch" selected.
   - Click **Commit changes**.
5. Verify: open the **Actions** tab → **TubePilot AI Daily
   Automation** → the workflow should show the new `shorts_count`
   input on **Run workflow**. Optionally trigger one manual run
   (leave Topic blank to pop from the queue, Shorts = 3) and check
   the run log shows `Starting TubePilot AI Daily Run` and the
   `Shorts stage:` summary line.

## Rollback

If the daily run misbehaves, re-open the same file in the web UI and
restore the previous version: **History** (clock icon) on the file
page → open the commit before this change → **View file** → pencil
icon → commit. That restores the old `dev_run.py` single-video run.
The Python code is harmless to leave merged (the old workflow never
calls it).

## First-run checklist (after applying)

- [ ] `yt_trends` and `yt_shorts` tabs appear in the "TubePilot AI
      Data" spreadsheet (created automatically by
      `db/setup_sheets.py` on the next run's "Verify Google Sheets
      Tabs & Config" step; if they don't appear, run that step's
      command once manually).
- [ ] Dashboard export still commits every 30 min
      (`dashboard.yml` untouched; the two new tabs appear in
      `dashboard-data.json` under `tabs` automatically).
- [ ] First daily run log contains: `Trend research for '...': N
      angles`, `Shorts stage: 3/3 uploaded`, and a Telegram success
      message with the real Shorts count.
