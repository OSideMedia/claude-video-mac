# claude-video-mac

[![Version](https://img.shields.io/badge/version-1.6.0-blue)](https://github.com/OSideMedia/claude-video-mac/releases)
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
upload, no length cap.** Nothing about the video ever leaves your machine — network is
used only to fetch things *in*: the video itself (if you give it a URL), the one-time
setup downloads (ffmpeg, Python deps), and Apple's on-device speech model the first
time a new locale is transcribed.

## What it does

Given a video URL (YouTube and most yt-dlp sites), a local video file (`.mp4/.mov/.mkv/
.webm/.m4v/.avi` — absolute, relative, or `~` path, or a folder containing one video), or
an audio-only file (podcasts work too), it produces the context Claude needs to reason
about it:

| Layer | Engine | Notes |
|---|---|---|
| **Decode + frames** | ffmpeg `-hwaccel videotoolbox` | Apple Silicon media engine |
| **Frame sampling** | ffmpeg `select` scene-cut **+** 2s time-floor **+** chapter starts | short-lived cards can't slip between samples |
| **Frame dedup** | perceptual hash + luminance check | static stretches collapse, distinct cards survive |
| **On-screen text** | Apple **Vision** (`VNRecognizeTextRequest`) | per-line confidence, parallel, per-frame fault tolerant |
| **Contact sheets** | AppKit/Quartz tiling, timestamp-labeled cells | one Read covers ~a dozen frames; long side capped at 1568 px |
| **Transcript** | native captions, else Apple **SpeechTranscriber** | on-device, macOS 26; caption track follows `--locale` |
| **Metadata** | yt-dlp (same call that resolves the id) | title, uploader, date, chapters — zero extra network |
| **Re-pull** | full-res re-extract + re-OCR | only for low-confidence frames |
| **Cache** | keyed by video id, per-window namespaces | follow-ups don't re-extract; assembly-only flags never do |

Claude gets a digest with a timestamped transcript, a timestamped on-screen-text layer,
the video's chapters, and frame image paths tagged `t=MM:SS` — and reads the frames to
actually *see* the video.

## Requirements

- macOS **26 (Tahoe)** or newer — for `SpeechAnalyzer`/`SpeechTranscriber`
- **Apple Silicon** (M-series)
- Python **3.11+** with `pyobjc-framework-Vision` / `-Quartz` (setup installs them into
  the interpreter that runs it)
- Xcode or Command Line Tools (Swift toolchain) — **only** to build the tiny
  SpeechTranscriber CLI; not needed when a prebuilt `transcribe` release asset is
  installed with `--transcribe-url` (see below)

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

Binaries are stored in `~/.local/share/claude-video-mac/bin/` (override: `WATCH_BIN_DIR`),
**outside both the plugin install and the data cache**, so they survive plugin updates
and `rm -rf ~/.cache/claude-video-mac`. Re-running setup after `claude plugin update` is
fast: it re-verifies the binaries (path, architecture, `-version`) and rebuilds the Swift
CLI **only if `main.swift` changed** since the last build. Versions 1.3–1.5 kept the
binaries in `~/.cache/claude-video-mac/bin/`; the pipeline still finds them there, and
the next `setup.py` run moves them to the new location.

To skip the Xcode requirement, install a prebuilt `transcribe` from a release (SHA-256
verified before it lands):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/watch/scripts/setup.py" \
  --transcribe-url https://github.com/OSideMedia/claude-video-mac/releases/download/v1.6.0/transcribe \
  --transcribe-sha256 <hash from the release notes>
```

(env equivalents: `WATCH_TRANSCRIBE_URL`, `WATCH_TRANSCRIBE_SHA256`). Maintainers build
the asset with `scripts/release-transcribe.sh`, which prints the hash and the
`gh release upload` command without running it.

Check the environment any time with:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/watch/scripts/watch.py" doctor
```

It prints the interpreter, each resolved binary with its architecture and version,
VideoToolbox, pyobjc, the speech locales and Vision languages, the cache size split into
media vs binaries, the URL-history size, and exits 1 if anything required is missing.

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
--locale xx-XX   transcription + OCR + caption-track locale (default en-US; en_US/en-us are
                 normalised; validated against SpeechTranscriber and Vision)
--summary-only   header + transcript + on-screen text, no frame/sheet paths
--no-cache       hard bypass: re-download and re-extract everything
--no-repull      skip the hi-res re-pull of low-confidence frames
--threshold N    OCR confidence below which a frame is re-pulled (0-1, default 0.5)
--purge          delete this video's cache dir and its url_ids.json entry, then exit
--purge-history  delete url_ids.json (the URL -> cache-id history), then exit
doctor           (in place of a source) print the environment report; exit 1 if unready
```

## Caching

Results are cached by video id under `~/.cache/claude-video-mac/` (override with
`WATCH_CACHE_DIR`), so follow-up questions about the same video are instant. Focused
`--start/--end` runs get their own `windows/` namespace and never invalidate the
full-video extraction. `--no-repull`, `--threshold` and `--summary-only` only change the
assembly, so they reuse the extraction too. The cache keeps the downloaded media and
grows with each new video — each run logs its current size (media only; binaries are
counted separately); reclaim space with `--purge` per video or by deleting the cache dir.

`url_ids.json` in the cache root is a plaintext history of every URL watched (it saves a
network round-trip on follow-ups). `--purge` removes a video's entry; `--purge-history`
deletes the file.

## Architecture

```
skills/watch/
  SKILL.md              the skill contract
  scripts/
    watch.py            orchestrator (frames+OCR ‖ transcript, then assemble) + cache + doctor
    common.py           shared config, binary resolution, locale/timestamp/cache conventions
    download.py         Phase 1 — yt-dlp / probe-in-place + locale-aware captions + site metadata
    frames.py           Phase 2 — VideoToolbox scene-aware extraction (+ chapter points) + dedup
    sheets.py           Phase 2b — timestamp-labeled contact sheets (AppKit/Quartz)
    ocr.py              Phase 3 — Apple Vision OCR layer (parallel, per-frame fault tolerant)
    transcribe.py       Phase 4 — captions or on-device SpeechTranscriber
    transcribe-swift/   Swift CLI wrapping SpeechAnalyzer/SpeechTranscriber (--locales, exit codes)
    assemble.py         Phase 5 — output contract + low-confidence hi-res re-pull
    doctor.py           environment report (watch.py doctor)
    setup.py            preflight + installer (+ legacy-binary migration, prebuilt transcribe)
  bin/                  pre-1.3.0 binary location (still honored as a fallback)
scripts/
  release-transcribe.sh build + sign the transcribe release asset, print its sha256
.claude-plugin/
  plugin.json           plugin manifest
  marketplace.json      single-plugin marketplace catalog
.github/workflows/
  tests.yml             ubuntu: py_compile + pytest on Python 3.11/3.12/3.13
tests/
  make_test_clip.sh     generates a deterministic test clip (scenes + text + speech)
  run_e2e.sh            end-to-end pipeline test against the clip (isolated cache)
  test_units.py         unit tests for the pure helpers (pytest or plain python3)
```

## Why Mac-native

- **Transcription** — Apple's Speech framework on macOS 26 (`SpeechAnalyzer` +
  `SpeechTranscriber`) runs a new on-device model: no key, no length cap, faster than
  cloud round-trips. Wrapped in a small native Swift CLI (the framework is Swift-only).
- **OCR** — Apple Vision does on-device text recognition with per-line confidence,
  callable from Python via `pyobjc-framework-Vision`.
- **Decode** — `-hwaccel videotoolbox` uses the Apple Silicon media engine.
- **Sampling** — `select='gt(scene,N)'` plus a 2s time-floor plus every chapter start,
  then perceptual dedup, so nothing is missed and nothing is wasted.

## Testing

```bash
python3 -m pytest tests/test_units.py -q   # pure-helper unit tests (also: python3 tests/test_units.py)
bash tests/run_e2e.sh                      # full-pipeline end-to-end suite (macOS 26, Apple Silicon)
```

The unit tests need no media and no setup; checks that need pyobjc skip themselves where
it is absent, so the same file runs on the Linux CI matrix. The e2e suite generates a
deterministic clip (4 scene cuts, known on-screen text, real speech via macOS `say`) and
runs the full pipeline against it in an isolated cache, asserting frames, OCR, transcript,
caching (including assembly-only flags and locale normalisation), cache-corruption
recovery, focused-window isolation, input validation, audio-only handling,
local-path/folder resolution, `--summary-only`, purge/history, and `doctor`.

## Troubleshooting

- **Start with `watch.py doctor`** — it names the interpreter in use and every binary the
  pipeline resolved; most "works in the terminal, fails in Claude" cases are two Pythons.
- **`missing components` from watch.py** — the message names `sys.executable` and the exact
  `pip install` command for that interpreter; or run
  `python3 skills/watch/scripts/setup.py` (re-running is safe and fast).
- **`pip install` fails with `externally-managed-environment`** — setup handles this
  automatically (PEP 668 / Homebrew Python) by retrying with
  `--user --break-system-packages`; if that's blocked too, use a venv:
  `python3 -m venv .venv && .venv/bin/python skills/watch/scripts/setup.py`.
- **`--locale` warnings** — a tag outside SpeechTranscriber's list (`doctor` lists them) is
  only a warning at start: captions and OCR still run, and the run fails at the
  transcription step only when the source has no captions. CJK/regional tags (`zh-CN`,
  `zh-TW`, `pt-PT`, `en-GB`…) reach Vision (mapped to `zh-Hans`/`zh-Hant` or passed
  through); a language Vision cannot OCR at all is a warning and on-screen text falls
  back to en-US.
- **First transcription of a new locale** downloads Apple's speech model once (needs
  network that one time; the transcribe CLI exits 3 if that download fails); inference
  is fully on-device thereafter.
- **ffmpeg SHA mismatch during setup** — the pinned upstream build rotated; review and
  re-pin in `setup.py`, or bypass with `WATCH_FFMPEG_SKIP_HASH=1` at your own risk.
- **`CERTIFICATE_VERIFY_FAILED` downloading ffmpeg** — common with python.org Python
  installs that haven't run "Install Certificates.command". Setup falls back to certifi,
  then to an unverified fetch (safe: the download is SHA-256-pinned); to fix it properly,
  run `/Applications/Python 3.x/Install Certificates.command`.
- **A playlist URL is refused** — pass one video's URL (with `v=`); the pipeline handles a
  single video per run.

## Third-party components

Fetched or installed at setup time, not distributed with this repo:

- [ffmpeg](https://ffmpeg.org) / ffprobe — native arm64 builds from
  [osxexperts.net](https://www.osxexperts.net), verified by pinned SHA-256 (ffmpeg is
  licensed LGPL/GPL by its authors)
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — video download + caption fetch + metadata
- [pyobjc](https://github.com/ronaldoussoren/pyobjc) — Python bridge to Apple's Vision
  and Quartz frameworks

## License

[MIT](LICENSE) © O-Side Media
