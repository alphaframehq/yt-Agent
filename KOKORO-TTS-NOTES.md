# Kokoro TTS Fallback — Notes (27 Nov 2026)

## Why

Runs #19/#20 died on Gemini TTS HTTP 429 with one key; run #22 died
the same way with **both** Gemini keys (2/2) at scene 11 of 14.
Gemini TTS is a free-tier API with a per-minute quota, so more keys
only delay the failure. Kokoro breaks the cycle: it runs entirely on
the runner's CPU (ONNX Runtime), needs no API key, and has no quota.

## Order (agents/tts.py `_synthesize`, TTS_PROVIDER=gemini default)

1. **Gemini TTS** (primary) — key pool GEMINI_KEY / GEMINI_KEY_2,
   21s pacing, 3 retry rounds, Retry-After honoured.
2. **Kokoro TTS** (fallback) — local, voice `af_heart`, no pacing.
3. **AWS Polly** (last) — only when AWS keys exist (they do not
   today); Polly is paid, Kokoro is free, so Kokoro goes first.
4. If all three fail, the **original Gemini error** is raised
   (fail-loud; never silent/stub audio).

`TTS_PROVIDER=polly` still means Polly-only (no Kokoro).

## Voice

- Gemini primary voice: Kore (female).
- Kokoro fallback voice: **`af_heart`** (female, the standard
  quality Kokoro voice). Mapping in `tools/kokoro_tts.py`:
  Joanna/Kore/Aoede -> af_heart, Matthew/Charon -> am_adam.
- A fallback scene may sound slightly different from its Gemini
  neighbours (af_heart is not Kore). Accepted over losing the
  whole video to a 429. Override with env `KOKORO_TTS_VOICE`.

## Files

- `tools/kokoro_tts.py` — the engine. Same `(path, duration)`
  contract as `tools/gemini_tts.py`: mono 16-bit WAV at 24 kHz,
  cache `cache/audio/` with a `kokoro:` hash prefix.
- `agents/tts.py` — wiring only (no signature change).
- `requirements.txt` — `kokoro-onnx==0.6.1` (Python 3.10–3.13;
  CI runs 3.11). Do NOT swap in the torch `kokoro` package: it is
  far heavier and the runner has no GPU.
- `tests/test_kokoro_tts.py` — mocked unit + wiring tests.
- `tests/test_gemini_tts.py`, `tests/test_media.py` — wiring
  tests updated to the new Gemini -> Kokoro -> Polly order.

## Model download

On first use each run, the engine downloads into `cache/kokoro/`:
- `kokoro-v1.0.onnx` (~310 MB)
- `voices-v1.0.bin` (~28 MB)
from the kokoro-onnx GitHub release (`model-files-v1.1`). The
runner is ephemeral, so this downloads on every run (a minute or
so at runner speeds); no workflow change is needed — the pip
install already flows through `requirements.txt`. A failed
download raises loudly; it never produces silence. Env override:
`KOKORO_MODEL_DIR`. Speed override: `KOKORO_TTS_SPEED` (default 1.0).

## Test locally

```bash
python -m venv /tmp/kokoro-venv && /tmp/kokoro-venv/bin/pip install \
  "kokoro-onnx==0.6.1" numpy loguru
KOKORO_MODEL_DIR=/tmp/kokoro-models python -c "
from tools.kokoro_tts import synthesize_speech
print(synthesize_speech('Hello world.', voice='Joanna'))
"
```

Real smoke test (27 Nov 2026, this VM, Python 3.12): 167 chars ->
11.56s WAV, mono 16-bit 24 kHz, 277,487 frames, ~13s wall time
including first model load; cache hit on the second call was
instant. Caveat: `/tmp` on this VM is a 512 MB tmpfs and filled
up mid-download — the model files live under
`~/workspace/tmp-kokoro/` for local testing, not `/tmp`.

## Known limits

- Kokoro synthesis is ~real-time on CPU (a 12s scene takes ~10s
  after the model is loaded). A 14-scene video adds a few minutes
  of TTS time when the fallback engages — still far cheaper than a
  failed run.
- The fallback voice differs from Kore; scenes that fall back are
  logged (`Kokoro TTS (fallback) synthesized ...`) so a run's log
  shows exactly which engine produced each scene.
