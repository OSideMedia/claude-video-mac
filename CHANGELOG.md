# Changelog

## 1.6.0 — 2026-09-26

Audit release: 13 bug fixes, 8 gaps closed, 5 new components. Every fix landed
behind a unit or e2e assertion that failed on 1.5.0 first.

### Fixed
- **Podcast cover art was treated as video** — ffprobe reports embedded album
  art as a `video` stream (`disposition.attached_pic=1`), so a `.m4a`/`.mp3`
  with artwork became a "64x64 @ 0fps" video and frames/OCR/sheets ran over
  the artwork. `probe()` skips attached pictures.
- **Empty transcripts say why**: "no speech detected" (transcription ran over
  the audio and found nothing), "no audio track", "empty caption track", or
  "transcription failed: …" — instead of "no captions and no audio" under a
  header that said `speechtranscriber (0 segments)`. A failed transcription
  now leaves a `source: error` record that is never reused.
- **`--locale` picked the wrong caption track**: `--sub-langs` fetched every
  listed track and `sorted(glob)[0]` chose alphabetically, so `fr-FR` (and
  it/ja/ko/pt/ru/zh) got `source.en.vtt`. Tracks are now ranked exact locale
  > same language > English fallback, and a leftover `.vtt` is reused only
  when it matches the locale or the same locale was requested before.
- **yt-dlp ran on every cache miss**, including `--start/--end` re-runs with
  `source.*` already on disk. The download is skipped unless `--no-cache`.
- **Binaries lived inside the data cache** (`~/.cache/claude-video-mac/bin`),
  so the documented "delete the cache dir" advice deleted 103 MB of
  ffmpeg/ffprobe/transcribe and the size log counted them. New default
  `~/.local/share/claude-video-mac/bin`; resolution order `WATCH_BIN_DIR` →
  new default → legacy `~/.cache/…/bin` → in-repo `skills/watch/bin` → PATH,
  so an updated plugin keeps working before `setup.py` is re-run; setup then
  MOVES the legacy binaries and removes the other legacy copies.
  `cache_size_bytes` no longer counts any bin dir; `--purge` never touches one.
- **Bare playlist URLs** downloaded the first entry while `--print` keyed the
  cache on the last. Every yt-dlp call carries `--playlist-items 1`; a `list=`
  URL without `v=` (or a `/playlist` path) is refused with a clear message.
- **One undecodable frame failed the whole OCR pass** (`CGImageSourceCreate
  ImageAtIndex` returns `None` for a truncated JPEG; `ex.map` had no per-frame
  guard). Each frame now records `lines: []` + `error`; `ocr.json` and the
  digest header count the failures. Found on the way: pyobjc resolves module
  attributes lazily and not thread-safely — the first concurrent lookups from
  the OCR pool intermittently raised `KeyError('CGImageSourceCreateWithURL')`;
  the symbols are now touched once on the calling thread before the pool starts.
- **Frame filenames past 1h** read as the wrong time (`1:02:03` →
  `t1m0203s`); now `t01h02m03s` (below an hour unchanged).
- **Portrait contact sheets rendered ~2048×1820**, past the ~1.5k vision cap
  (frames are extracted 512 wide in both orientations). The sheet's long side
  is capped at 1568 px and the geometry comment describes the real cell sizes.
- `--` guards the source on every yt-dlp invocation.
- `parse_ts` rejected nothing: `nan`, `inf`, `1e5`, and `1:-30` (→ 30 s)
  all parsed. Components must be unsigned decimals; numbers finite and ≥ 0.
- `watch.md` is written atomically; the intermediate wav is unlinked even when
  the transcriber fails; `setup.py` no longer IndexErrors on an empty
  `swift --version`.
- **`--purge` left the URL in `url_ids.json`** — a plaintext watch history the
  docs implied was gone. `--purge` drops the entry; `--purge-history` deletes
  the file; the docs say what it is.

### Added
- **`tests/test_units.py` is pytest-collectable** (`def test_*`) and still runs
  as a script; 41 groups, framework-dependent checks skip without pyobjc.
- **Preflight names the interpreter**: the "missing components" error prints
  `sys.executable` and the exact `"<python>" -m pip install …` fix (three
  python3s commonly coexist).
- **BCP-47 locale normalisation** (`en_US`/`en-us` → `en-US`) before the
  cache key, and **validation** against `SpeechTranscriber.supportedLocales`
  (error listing the supported set) and Vision's OCR languages (warning; OCR
  falls back to en-US).
- **transcribe CLI**: segments are returned from the collector Task (the old
  captured-`var` pattern is a Swift 6 language-mode error; the file now builds
  under `-swift-version 6`); exit codes 2 usage / 3 speech model unavailable
  (says it needs network once) / 4 transcription failure; `transcribe
  --locales` prints the supported locales as JSON; the install-model message
  only prints when `AssetInventory.status` is not `.installed`.
- **Assembly-only flags leave the cache key**: `--no-repull`/`--threshold`
  follow-ups (and `--summary-only`) reuse the extraction instead of
  re-running frames + OCR + ASR.
- **setup.py**: skips the Swift rebuild when the binary exists and a stored
  SHA-256 of `main.swift` is unchanged; architecture check (`file`/`lipo`) on
  every pre-existing binary, not just `-version`; downloads use a 60 s
  socket timeout; the zip is removed in `finally` and a bad archive is a
  reported failure rather than a traceback.
- **First failure wins**: the frames+OCR and transcript phases run under
  `wait(FIRST_EXCEPTION)`; a failed transcription surfaces immediately instead
  of after a minutes-long OCR pass, which is told to stop.
- **Smaller digest**: the frames dir is printed once and each frame line is
  `t=MM:SS  basename` (~300 repeated absolute paths gone); SKILL.md tells
  Claude to join dir + basename. The showinfo desync grid fallback is now a
  `timestamps_estimated` flag in `frames.json` and a header line in the digest.
- **`--summary-only`**: header + Video/Chapters + transcript + on-screen text,
  no frame/sheet paths; served from the cache without re-extracting.
- **Chapters + site metadata** from the ONE yt-dlp id call: the `--print`
  template also emits title, uploader, upload_date, duration, description and
  `%(chapters)j` (tab-separated, JSON-encoded fields, parsed robustly);
  stored in `meta.json`; `## Video` and `## Chapters` blocks in the digest;
  chapter starts are forced frame-sample points. Zero extra network; local
  files simply have none.
- **`watch.py doctor`**: interpreter, each resolved binary with path/arch/
  version, VideoToolbox, pyobjc, speech locales, Vision languages, cache
  split media/bin, `url_ids.json` count, bin dir in use; exit 1 on a missing
  prerequisite.
- **CI** (`.github/workflows/tests.yml`): ubuntu, Python 3.11/3.12/3.13,
  `py_compile` + pytest. No macOS job — SpeechTranscriber needs macOS 26 and
  no GitHub-hosted runner label for it is verified; e2e stays local.
- **Prebuilt transcribe**: `scripts/release-transcribe.sh` builds, ad-hoc
  signs, prints the sha256 and the `gh release upload` command (not run);
  `setup.py --transcribe-url URL --transcribe-sha256 HEX` (or
  `WATCH_TRANSCRIBE_URL`/`_SHA256`) installs it after verification — no Xcode
  needed when a release asset exists.
- e2e suite grows to 33 assertions (assembly-only cache hits, locale
  normalisation, `--summary-only`, frames-dir layout, purge/history, doctor,
  bad timestamps).

### Fix round (independent review of the release candidate, same day)
- **CJK and regional on-screen text was lost** (regression introduced in
  this release's locale work): OCR kept a tag only if it matched Vision's
  list literally, but Vision spells Chinese by script (`zh-Hans`, `zh-Hant`)
  while SpeechTranscriber and users spell it by region (`zh-CN`, `zh-TW`), so
  `--locale zh-CN` ran OCR in en-US only and read nothing (measured: Vision
  reads 中文识别测试 with `zh-CN`/`zh-Hans`, nothing with `en-US`). Regional
  Latin tags (`pt-PT`, `en-GB`, `fr-CA`, `de-AT`, `es-MX`, `it-CH`) were cut
  too. Now: region-only CJK maps to its script form, any locale whose
  language Vision knows is passed through as requested, and only an unknown
  language falls back to en-US.
- **Captioned sources in Arabic/Russian/Thai/… were refused** (regression):
  the locale gate raised for any tag outside SpeechTranscriber's list even
  when the transcript would come from captions and OCR from Vision. The gate
  is a warning at start; the hard failure moved into `transcribe.py`, right
  before the CLI is invoked, and says captions/OCR are not limited by it.
- **A failed phase no longer waits for its sibling's subprocess**: the
  survivor's ffmpeg/transcribe child is polled against the stop event and
  killed; the executor shuts down with `cancel_futures=True` (measured before:
  error at 0 s, exit after the 4 s sibling).
- `setup.py` migration verifies the destination binary RUNS (`-version` /
  usage exit) before deleting legacy copies, and replaces a native-but-broken
  destination with a working legacy copy instead of forcing a re-download.
- A prebuilt `transcribe` is stamped with its own asset SHA-256 (not the
  current `main.swift` hash), so an older asset never reads as "built from
  this source"; an intact asset counts as up to date.
- Chapter-start frames are exempt from perceptual dedup and thinning and are
  marked `(chapter start)`, so "every chapter start gets a frame" holds.
- `parse_ts` rejects minutes/seconds fields ≥ 60 (`1:60` parsed to 120 s);
  `/embed/videoseries?list=` counts as a bare playlist; `transcript.vtt` is
  written atomically; SKILL.md no longer says only *manual* captions are used.
- Tests: the `--summary-only` cache-miss path is pinned (watch.md on disk
  must stay the FULL digest); the warning path and the transcription-time
  refusal are covered with a fake locale list (the gate is inert on a
  machine whose `transcribe` predates `--locales`).
- **A bare `--locale en` (or `fr`, `de`, `zh`…) failed at the transcription
  step** after all the frame/OCR work: the Python checks treated `en` as
  covered by `en-US`, but the CLI matches exactly and exited 3. A bare
  language is now resolved to a full locale before the cache key (default
  table en→en-US, fr→fr-FR, de→de-DE, es→es-ES, pt→pt-BR, it→it-IT,
  ja→ja-JP, ko→ko-KR, zh→zh-CN, yue→yue-CN, else the first supported tag of
  that language; the choice is logged), the speech gate is an exact match,
  and the transcribe CLI itself falls back to the language's likely region,
  then any supported locale of that language, before exiting 3.
- A prebuilt `transcribe`'s sidecar hash is taken AFTER the ad-hoc re-sign
  (which can rewrite the bytes), so the installed binary no longer reads as
  stale and a later setup run no longer demands Swift.
- Chapter-protected thinning keeps the first and last frame too; with more
  chapters than `--max-frames` the effective cap is chapters + 2.
- A chapter just before a `--start/--end` window no longer marks the
  window's first frame "(chapter start)"; only chapters inside the window
  count. The digest label itself is now asserted.

### Housekeeping
- README/SKILL.md: bin-dir location and the "instant" re-setup claim
  corrected, cache-deletion advice fixed, flags table matches argparse,
  `doctor`/prebuilt/chapters documented; `.pytest_cache/` gitignored.
- Extraction contract bumped to 1.6.0 (frame names, digest layout, chapter
  points, locale in the key), so 1.5.0 caches regenerate on first use.

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
