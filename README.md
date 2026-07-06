# claude-video-mac

[![Version](https://img.shields.io/badge/version-1.2.1-blue)](https://github.com/OSideMedia/claude-video-mac/releases)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Claude%20Code-purple)](https://github.com/OSideMedia/claude-video-mac)
[![macOS](https://img.shields.io/badge/macOS-26%2B%20(Tahoe)-black?logo=apple)](https://github.com/OSideMedia/claude-video-mac#requirements)
[![Apple Silicon](https://img.shields.io/badge/Apple%20Silicon-arm64-black?logo=apple)](https://github.com/OSideMedia/claude-video-mac#requirements)
[![On-device](https://img.shields.io/badge/inference-100%25%20on--device-orange)](https://github.com/OSideMedia/claude-video-mac#why-mac-native)

**Give Claude Code the ability to watch a video — entirely on-device, on Apple Silicon.**

A Mac-native successor to the portable [`/watch`](https://github.com/bradautomates/claude-video)
skill: it replaces that tool's lowest-common-denominator pipeline with on-device Apple
Silicon pipelines, packaged as a Claude Code plugin installable across all your projects.

Everything runs locally on the Apple Silicon media + neural engines: **no API keys, no
upload, no length cap.** Nothing about the video ever leaves your machine (the only
network traffic is downloading the video itself, if you give it a URL).

## What it does

Given a video URL (YouTube and most yt-dlp sites), a local video file (`.mp4/.mov/.mkv/
.webm/.m4v/.avi` — absolute, relative, or `~` path, or a folder containing one video), or
an audio-only file (podcasts work too), it produces the context Claude needs to reason
about it:

| Layer | Engine | Notes |
|---|---|---|
| **Decode + frames** | ffmpeg `-hwaccel videotoolbox` | Apple Silicon media engine |
| **Frame sampling** | ffmpeg `select` scene-cut **+** 2s time-floor | short-lived cards can't slip between samples |
| **Frame dedup** | perceptual hash + luminance check | static stretches collapse, distinct cards survive |
| **On-screen text** | Apple **Vision** (`VNRecognizeTextRequest`) | per-line confidence |
| **Transcript** | native captions, else Apple **SpeechTranscriber** | on-device, macOS 26 |
| **Re-pull** | full-res re-extract + re-OCR | only for low-confidence frames |
| **Cache** | keyed by video id, per-window namespaces | follow-ups don't re-extract |

Claude gets a digest with a timestamped transcript, a timestamped on-screen-text layer,
and frame image paths tagged `t=MM:SS` — and reads the frames to actually *see* the video.

## Requirements

- macOS **26 (Tahoe)** or newer — for `SpeechAnalyzer`/`SpeechTranscriber`
- **Apple Silicon** (M-series)
- Python **3.11+**
- Xcode or Command Line Tools (Swift toolchain, to build the tiny SpeechTranscriber CLI)

## Install

As a Claude Code plugin:

```text
/plugin marketplace add OSideMedia/claude-video-mac
/plugin install claude-video-mac@claude-video-mac
```

Then, once, install the local components (native arm64 ffmpeg, Swift CLI, Python deps):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/watch/scripts/setup.py"
```

The skill triggers when you share a video and ask what's in it, or invoke
`/claude-video-mac:watch`.

### Use it directly (without installing the plugin)

```bash
git clone https://github.com/OSideMedia/claude-video-mac
cd claude-video-mac
python3 skills/watch/scripts/setup.py                 # one-time
python3 skills/watch/scripts/watch.py "<URL-or-path>" # run
```

Stdout is the digest; progress goes to stderr.

## Options

```text
--scene N        scene-cut sensitivity, 0-1 (default 0.3; lower = more frames)
--floor S        sample static shots at least every S seconds (default 2s, hard-capped at 2s)
--width PX       frame width (default 512)
--max-frames N   cap, evenly thinned if exceeded (default 300)
--start / --end  focus a window: densely re-extract just that span (SS, MM:SS, or HH:MM:SS)
--locale xx-XX   transcription + OCR locale (default en-US)
--no-cache       hard bypass: re-download and re-extract everything
--no-repull      skip the hi-res re-pull of low-confidence frames
--purge          delete this video's cache dir and exit
```

## Caching

Results are cached by video id under `~/.cache/claude-video-mac/` (override with
`WATCH_CACHE_DIR`), so follow-up questions about the same video are instant. Focused
`--start/--end` runs get their own `windows/` namespace and never invalidate the
full-video extraction. The cache keeps the downloaded media and grows with each new
video — each run logs its current size; reclaim space with `--purge` per video or by
deleting the cache dir.

## Architecture

```
skills/watch/
  SKILL.md              the skill contract
  scripts/
    watch.py            orchestrator (frames+OCR ‖ transcript, then assemble) + cache
    common.py           shared config, binary paths, timestamp + cache conventions
    download.py         Phase 1 — yt-dlp / probe-in-place + best-effort captions
    frames.py           Phase 2 — VideoToolbox scene-aware extraction + perceptual dedup
    ocr.py              Phase 3 — Apple Vision OCR layer
    transcribe.py       Phase 4 — captions or on-device SpeechTranscriber
    transcribe-swift/   Swift CLI wrapping SpeechAnalyzer/SpeechTranscriber
    assemble.py         Phase 5 — output contract + low-confidence hi-res re-pull
    setup.py            preflight + installer
  bin/                  native arm64 ffmpeg, ffprobe, transcribe (fetched/built; gitignored)
.claude-plugin/
  plugin.json           plugin manifest
  marketplace.json      single-plugin marketplace catalog
tests/
  make_test_clip.sh     generates a deterministic test clip (scenes + text + speech)
  run_e2e.sh            end-to-end pipeline test against the clip (isolated cache)
```

## Why Mac-native

- **Transcription** — Apple's Speech framework on macOS 26 (`SpeechAnalyzer` +
  `SpeechTranscriber`) runs a new on-device model: no key, no length cap, faster than
  cloud round-trips. Wrapped in a small native Swift CLI (the framework is Swift-only).
- **OCR** — Apple Vision does on-device text recognition with per-line confidence,
  callable from Python via `pyobjc-framework-Vision`.
- **Decode** — `-hwaccel videotoolbox` uses the Apple Silicon media engine.
- **Sampling** — `select='gt(scene,N)'` plus a 2s time-floor, then perceptual dedup, so
  nothing is missed and nothing is wasted.

## Testing

```bash
bash tests/run_e2e.sh
```

Generates a deterministic clip (4 scene cuts, known on-screen text, real speech via
macOS `say`) and runs the full pipeline against it in an isolated cache — 14 assertions
over frames, OCR, transcript, caching, focused-window isolation, input validation, and
audio-only handling.

## Troubleshooting

- **`pip install` fails with `externally-managed-environment`** — setup handles this
  automatically (PEP 668 / Homebrew Python) by retrying with
  `--user --break-system-packages`; if that's blocked too, use a venv:
  `python3 -m venv .venv && .venv/bin/python skills/watch/scripts/setup.py`.
- **First transcription of a new locale** downloads Apple's speech model once (needs
  network that one time); inference is fully on-device thereafter.
- **ffmpeg SHA mismatch during setup** — the pinned upstream build rotated; review and
  re-pin in `setup.py`, or bypass with `WATCH_FFMPEG_SKIP_HASH=1` at your own risk.
- **"missing components" error from watch.py** — run
  `python3 skills/watch/scripts/setup.py` (re-running is safe and fast).

## Third-party components

Fetched or installed at setup time, not distributed with this repo:

- [ffmpeg](https://ffmpeg.org) / ffprobe — native arm64 builds from
  [osxexperts.net](https://www.osxexperts.net), verified by pinned SHA-256 (ffmpeg is
  licensed LGPL/GPL by its authors)
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — video download + caption fetch
- [pyobjc](https://github.com/ronaldoussoren/pyobjc) — Python bridge to Apple's Vision
  and Quartz frameworks

## License

[MIT](LICENSE) © O-Side Media
