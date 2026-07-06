# Changelog

## 1.3.0 — 2026-07-05

### Changed
- **Native binaries now live in `~/.cache/claude-video-mac/bin/`** (override:
  `WATCH_BIN_DIR`) instead of inside the plugin install, so they **survive
  plugin updates** — no more 100MB re-download + setup after every
  `claude plugin update`. Setup migrates working binaries from a pre-1.3.0
  in-install `bin/` automatically; the legacy location is still honored as a
  fallback. The binary dir is deliberately independent of `WATCH_CACHE_DIR`,
  so relocating the data cache can't orphan the binaries.

## 1.2.3 — 2026-07-05

### Fixed
- Full-video runs no longer record (and display) a bogus "focused window"
  spanning the whole video — the window banner now appears only for explicit
  `--start/--end` runs, so the "coverage before absence" guidance can't be
  wrongly triggered on full extractions. E2E asserts the banner's absence
  (18 assertions).

## 1.2.2 — 2026-07-05

### Fixed
- `setup.py` no longer crashes with a raw traceback when the ffmpeg download
  hits an SSL verification failure (common with python.org Python installs
  that haven't run "Install Certificates.command"). It now falls back to
  certifi's CA bundle, then — only while the SHA-256 pin is enforced — to an
  unverified fetch, and reports download failures cleanly.

## 1.2.1 — 2026-07-05

### Added
- **Friendlier local-source handling**: `~` paths are expanded, relative and
  absolute spellings of the same file share one cache entry, and pointing at a
  **folder** works — it resolves to the single media file inside, or lists the
  candidates if there's more than one.
- README badges (version / license / platform / macOS / Apple Silicon /
  on-device).
- E2E coverage for folder input, relative-path cache identity, and ambiguous
  folders (17 assertions total).

## 1.2.0 — 2026-07-05

Full audit release: bug fixes, cache hardening, audio-only support, and an
automated end-to-end test suite.

### Fixed
- **Perceptual dedup no longer drops distinct cards.** The difference hash is
  now paired with an absolute-luminance check, so two cards with the same
  layout on different background colors are kept as distinct frames.
- Transcript reuse validates the requested locale — a `--locale` change can no
  longer serve a stale transcript in the wrong language.
- yt-dlp is pointed at the bundled ffmpeg (`--ffmpeg-location`), fixing DASH
  format merging and subtitle conversion on machines without a system ffmpeg.
- Resolved URL→video-id mappings are persisted, so cached follow-ups skip the
  network entirely and a transient rate limit can't silently change the cache
  key and orphan the cache.
- Invalid focus windows (`--end` before `--start`, `--start` past the end of
  the video) are rejected with a clear error instead of extracting the wrong
  range.
- `setup.py` handles PEP 668 (Homebrew Python) by retrying with
  `--user --break-system-packages`.
- The 2-second sampling-floor cap is enforced on user-provided `--floor`
  values, matching the documented contract.

### Added
- **Audio-only sources** (podcasts, music, no-video streams) are supported:
  frames + OCR are skipped and the digest is transcript-only, and says so.
- **Per-window cache namespaces**: focused `--start/--end` artifacts live under
  `windows/<span>/`, so a focused re-run never invalidates the full-video
  extraction (and vice versa).
- `--purge` flag to delete a video's cache dir; cache size is logged each run.
- OCR recognition language follows `--locale` (with en-US fallback).
- Friendly preflight: missing components produce a "run setup.py" message
  instead of a traceback.
- `tests/run_e2e.sh`: 14-assertion end-to-end suite covering frames, OCR,
  transcript, caching, window isolation, input validation, and audio-only
  handling against the deterministic test clip.

## 1.1.0 — 2026-06-03

- Cap the static-sampling floor at 2s so short-lived on-screen cards can't
  fall between samples.
- Perceptual-hash (dhash) dedup collapses near-identical frames.
- Focused-window extraction (`--start`/`--end`) for dense re-inspection of a
  specific span.
- `--no-cache` is a true hard bypass (re-download + re-extract).
- "Coverage before absence" guidance in SKILL.md.

## 1.0.0 — 2026-06-03

Initial release: on-device Apple Silicon pipeline — VideoToolbox decode,
scene-aware frame sampling, Apple Vision OCR, native captions or on-device
SpeechTranscriber, low-confidence hi-res re-pull, per-video caching.
