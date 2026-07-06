# claude-video-mac

A Mac-native successor to the [`/watch`](https://github.com/bradautomates/claude-video)
skill. Gives Claude the ability to **watch a video** — but replaces that tool's portable,
lowest-common-denominator pipeline with **on-device Apple Silicon** pipelines, packaged as
a Claude Code plugin installable across all your projects.

Everything runs locally on the Apple Silicon media + neural engines: **no API keys, no
upload, no length cap.**

## What it does

Given a video URL (YouTube and most yt-dlp sites) or a local file, it produces the context
Claude needs to reason about the video:

| Layer | Engine | Notes |
|---|---|---|
| **Decode + frames** | ffmpeg `-hwaccel videotoolbox` | Apple Silicon media engine |
| **Frame sampling** | ffmpeg `select` scene-cut **+** time-floor | static shots still sampled |
| **On-screen text** | Apple **Vision** (`VNRecognizeTextRequest`) | per-line confidence |
| **Transcript** | native captions, else Apple **SpeechTranscriber** | on-device, macOS 26 |
| **Re-pull** | full-res re-extract + re-OCR | only for low-confidence frames |
| **Cache** | keyed by video id | follow-ups don't re-extract |

## Requirements

- macOS **26 (Tahoe)** or newer
- **Apple Silicon** (M-series)
- Python **3.11+**, Xcode / Swift toolchain (for the SpeechTranscriber CLI)

## Install (as a plugin)

```text
/plugin marketplace add OSideMedia/claude-video-mac
/plugin install claude-video-mac@claude-video-mac
```

Then, once, install the local components (native ffmpeg, Swift CLI, Python deps):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/watch/scripts/setup.py"
```

The skill triggers when you share a video and ask what's in it, or invoke
`/claude-video-mac:watch`.

## Use it directly (without installing)

```bash
python3 skills/watch/scripts/setup.py                 # one-time
python3 skills/watch/scripts/watch.py "<URL-or-path>" # run
```

Stdout is a digest with **Transcript**, **On-screen text**, and **Frames** (JPEG paths
tagged `t=MM:SS`). Read those frames to see the video.

## Architecture

```
skills/watch/
  SKILL.md              the skill contract
  scripts/
    watch.py            orchestrator (frames+OCR ‖ transcript, then assemble) + cache
    common.py           shared config, binary paths, timestamp + cache conventions
    download.py         Phase 1 — yt-dlp / probe-in-place + best-effort captions
    frames.py           Phase 2 — VideoToolbox scene-aware frame extraction
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
- **Sampling** — `select='gt(scene,N)'` plus a time-floor so nothing is missed.

## License

MIT
