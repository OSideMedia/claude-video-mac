---
name: watch
description: Watch a video on-device on Apple Silicon — pull frames, on-screen text (OCR), and a timestamped transcript so you can answer questions about what happens in it. Use when the user shares a video URL (YouTube, etc.) or a local video file and asks what's in it, to summarize/analyze/describe it, find a moment, or read on-screen text. macOS 26+ (Tahoe), Apple Silicon only.
---

# Watch a video (Mac-native)

Turns a video into something you can reason about: sampled **frames**, a timestamped
**on-screen-text layer** (Apple Vision OCR), and a timestamped **transcript** (native
captions if present, else Apple's on-device SpeechTranscriber). Everything runs locally
on the Apple Silicon media + neural engines — no API keys, no upload, no length cap.

## When to use
The user gives a video URL or local path and wants to know what's in it, a summary, a
specific moment, spoken content, or on-screen text. Works for YouTube and most yt-dlp
sites, and for local `.mp4/.mov/.mkv/.webm`.

## Prerequisites (one-time)
Before the first run, install the local components:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/watch/scripts/setup.py"
```

This checks the environment (macOS 26+, Apple Silicon, Python 3.11+, Swift), installs the
Python deps (pyobjc Vision/Quartz, yt-dlp), fetches a native arm64 ffmpeg/ffprobe into the
skill's `bin/`, and builds the Swift SpeechTranscriber CLI. Re-running is safe and fast.
If `setup.py` reports a failure, relay it to the user and stop.

## Run it
```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/watch/scripts/watch.py" "<URL-or-local-path>"
```

The command prints a digest to stdout with three sections — **Transcript**, **On-screen
text**, and **Frames**. The Frames section lists JPEG paths each tagged `t=MM:SS`.

**Then read the frame images** at those paths (use the Read tool on each `.jpg`) so you can
see the video, and combine what you see with the transcript and OCR text to answer the
user. Frames flagged `(hi-res re-pull)` are sharper re-extractions of moments where OCR
confidence was low — prefer those.

Frames are sampled densely (at least every ~2s, plus every scene cut) and then
near-identical frames are collapsed with a perceptual hash, so a brief on-screen card
won't fall between samples while static talking-head stretches stay compact.

## Answering "what's on screen" — coverage before absence
A **negative** claim ("there's no card / nothing is shown / the screen is just the host")
is only valid if frames actually **cover that moment at adequate density**. Before asserting
absence:
- Check the Frames list for frames within ~1–2s of the moment in question. On-screen cards
  in tutorials are often up for only ~3s — a single nearby frame is not enough if the gap to
  its neighbors is several seconds.
- If the digest shows a **focused window**, remember its frames cover only that range; say
  nothing about moments outside it.
- If coverage is sparse around the moment, or the frames were cached from a wider/sparser
  pass, **do not assert absence**. Say the coverage is thin and re-extract that span:

  ```bash
  python3 "${CLAUDE_PLUGIN_ROOT}/skills/watch/scripts/watch.py" "<URL-or-path>" --start MM:SS --end MM:SS
  ```

  Then read the new dense frames before answering. Prefer confirming what *is* shown over
  asserting what *isn't* from partial coverage.

## Options
- `--scene N` scene-cut sensitivity, 0–1 (default 0.3; lower = more frames).
- `--floor S` sample static shots at least once per S seconds (default 2s, capped at 2s).
- `--width PX` frame width (default 512).
- `--max-frames N` cap (default 300; evenly thinned if exceeded).
- `--start MM:SS` / `--end MM:SS` focus a window — densely re-extract just that span to
  inspect a specific moment closely. Accepts `SS`, `MM:SS`, or `HH:MM:SS`.
- `--locale xx-XX` transcription locale (default en-US).
- `--no-cache` hard bypass: re-download and re-extract, ignoring any cached result.
- `--no-repull` skip the hi-res re-pull of low-confidence frames.

## Caching
Results are cached by video id under `~/.cache/claude-video-mac/`. A follow-up question
about the same video reuses the extracted frames/transcript instantly — just re-run the
same command (it returns the cached digest) and read the frames again.

The cache key includes the focus window: a `--start/--end` run always performs a fresh
focused extraction for that span and is never served a digest computed from the full video
(or a different window). `--no-cache` bypasses the cache entirely. Use a focused re-run
whenever you need to confirm or rule out something at a specific timestamp.

## Notes
- First transcription of a new locale downloads Apple's speech model once; inference itself
  is fully on-device and offline thereafter.
- If a URL has manual captions they're used as the transcript; otherwise audio is
  transcribed on-device. Caption fetch failures (e.g. rate limits) never block the run.
- The artifacts for a video live in its cache dir: `frames/`, `transcript.vtt`,
  `transcript.json`, `ocr.json`, `frames.json`, `meta.json`, and the assembled `watch.md`.
