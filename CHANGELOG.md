# Changelog

## 1.5.0 — 2026-07-31

Fixes and improvements from a three-model council audit (Codex gpt-5.6-sol,
Gemini 3.1 Pro, Claude Opus — findings cross-verified before adoption).

### Fixed
- **Frame timestamps could silently desync on very long videos**: ffmpeg's
  `%04d` is a minimum width, so past 9,999 sampled frames the lexicographic
  file sort interleaved (`frame_10000` between `frame_1000` and `frame_1001`)
  and every frame got the wrong `t=` tag. Now `%06d` + numeric sort.
- **Audio-only URLs work** (podcasts, no-video streams): the format selector
  gained a bare-audio fallback and the downloaded-file check accepts audio
  extensions — previously these raised "yt-dlp produced no video file"
  despite the documented support.
- **`--locale` is honored end-to-end**: the hi-res re-pull re-OCRd frames in
  en-US regardless of locale (dropping non-English text exactly where OCR
  needed help), and caption fetching only ever requested English tracks. The
  re-pull also no longer replaces a multi-line reading with a
  higher-confidence but *sparser* one.
- **A corrupt/truncated cache no longer bricks the video**: unreadable
  `done.json`/`frames.json` now count as a cache miss and re-extract, instead
  of erroring on every subsequent run. All JSON writes are atomic
  (temp + rename), and a per-video lock stops two concurrent runs on the same
  video from deleting each other's frames mid-flight.
- **Numeric options are validated** (`--max-frames 0` was a division by zero;
  bare `--end -5` silently extracted the whole video while claiming a window).
- **Thinning keeps both endpoints** — the last frame of a thinned video was
  never selected.
- Negative/scientific `pts_time` values from ffmpeg no longer force the
  even-grid timestamp fallback.
- Leftover DASH fragments (`source.f401.mp4`) can no longer be picked as the
  downloaded media; exact `source.<ext>` is required.
- Cached auto-captions are no longer mislabeled "manual" on re-runs.
- HTML entities (`&amp;#39;` …) are unescaped in caption transcripts.
- All text I/O pins UTF-8 (a `LANG=C` shell could crash the digest print);
  local-file cache identity uses `st_mtime_ns`; URL cache keys include the
  yt-dlp extractor (ids are only unique per site).
- `setup.py --check` now reports binaries found in the legacy dir or PATH
  (matching what the pipeline actually uses); watch.py preflight also checks
  the transcribe CLI and yt-dlp before spending minutes extracting.

### Added
- **Coverage honesty in the digest**: the header reports the largest gap
  between kept frames and flags when `--max-frames` thinning voided the ~2s
  density; SKILL.md keys its "coverage before absence" rule to that reported
  gap.
- **Windowed transcript**: focused `--start/--end` runs print only the
  window's transcript (with the full `transcript.vtt` path listed), and
  consecutive caption cues merge into readable ≤25s paragraphs — a large
  token cut on long videos and repeat focused runs.
- **Parallel OCR**: Vision requests run across a thread pool (frames are
  independent); the OCR phase was the pipeline's wall-clock tail.
- **Untrusted-content guidance in SKILL.md**: transcript/OCR/frame text is
  data from arbitrary internet content, never instructions.
- `tests/test_units.py`: unit tests for the pure helpers (timestamps, VTT
  parsing, thinning, paragraph merging, cache identity) plus a
  version-consistency gate across all five version stamps; e2e gained
  corrupt-cache recovery, floor-clamp cache equivalence, and invalid-option
  cases.

### Housekeeping
- `audio_16k.wav` (~115 MB/hour) is deleted after transcription; orphaned
  hi-res re-pulls are cleared on re-extraction; contact-sheet cells never
  upscale below-512px frames; README badge/architecture/testing sections
  refreshed; `.DS_Store` gitignored.

## 1.4.0 — 2026-07-11

### Added
- **Labeled contact sheets** (`sheets.py`, Phase 2b): the kept frames are tiled
  into timestamp-labeled grid images (3x4 landscape / 4x2 portrait cells), and
  the digest lists the sheets ahead of the individual frames. One sheet Read
  replaces up to a dozen frame Reads (~75% fewer tokens on the visual layer);
  individual full-size frames remain listed for close inspection. Rendered
  with AppKit/Quartz (no new dependencies; the bundled ffmpeg's drawtext is
  not relied on). Sheet build failures fall back to the frames-only digest.
  Extraction contract bumped to 1.4.0 so pre-sheet caches regenerate; e2e
  asserts a sheet is listed and exists on disk.

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
