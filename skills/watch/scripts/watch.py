"""watch.py — entry point orchestrating the Mac-native /watch pipeline.

    python3 watch.py <url-or-path> [options]

Phases: download/probe -> (frames -> OCR) ‖ transcript -> assemble.
Frames+OCR and the transcript are independent, so they run concurrently.
Audio-only sources (podcasts, no-video streams) skip frames+OCR and still
produce a transcript digest.

Cache (Phase 6): results are keyed by video id under ~/.cache/claude-video-mac/.
Full-video artifacts live in the video's work dir; each focused --start/--end
window gets its own windows/<span>/ subdir, so a focused pass never clobbers
the full-video extraction. A completed run drops done.json recording the
parameters; a later invocation with matching parameters reprints the cached
digest without re-extracting.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import shutil
import subprocess
import sys
import threading
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from pathlib import Path

import assemble as assemble_mod
import download as download_mod
import frames as frames_mod
import transcribe as transcribe_mod
from common import (
    CACHE_ROOT,
    FFMPEG,
    FFPROBE,
    SCRIPTS_DIR,
    TRANSCRIBE,
    URL_ID_MAP,
    VERSION_TAG,
    artifact_dir,
    cache_size_bytes,
    chapter_starts,
    locale_matches,
    log,
    normalize_locale,
    parse_ts,
    read_json,
    resolve_source,
    video_id_for,
    video_lock,
    work_dir,
    write_json,
)

# `start`/`end` are part of the key: a focused-window run must never be served a
# digest computed from a different (e.g. full-video, sparser) window, and vice versa.
# `repull`/`threshold` are NOT: they only steer assembly, so a --no-repull
# follow-up must reuse the extraction instead of re-running frames+OCR+ASR.
CACHE_KEYS = ("scene", "floor", "width", "max_frames", "locale", "start", "end")


def _preflight(is_url: bool = False) -> None:
    """Friendly failures instead of tracebacks when setup.py hasn't run.
    Checks everything the pipeline may need BEFORE the expensive phases, so a
    missing transcribe binary can't surface only after minutes of extraction."""
    problems = []
    pip_pkgs = []
    for name, path in (("ffmpeg", FFMPEG), ("ffprobe", FFPROBE)):
        if not Path(path).exists() and shutil.which(name) is None:
            problems.append(f"{name} not found")
    for mod in ("Vision", "Quartz"):
        if importlib.util.find_spec(mod) is None:
            problems.append(f"pyobjc-framework-{mod} not installed")
            pip_pkgs.append(f"pyobjc-framework-{mod}")
    if not Path(TRANSCRIBE).exists():
        problems.append("transcribe CLI not built")
    if is_url and shutil.which("yt-dlp") is None and importlib.util.find_spec("yt_dlp") is None:
        problems.append("yt-dlp not installed (needed for URL sources)")
        pip_pkgs.append("yt-dlp")
    if problems:
        # Several python3s commonly coexist (python.org, Homebrew, Xcode); the
        # deps must land in THIS one, so name it and give the exact command.
        msg = ("missing components: " + ", ".join(problems)
               + f"\n  interpreter: {sys.executable} (Python {sys.version.split()[0]})")
        if pip_pkgs:
            msg += f'\n  fix deps:    "{sys.executable}" -m pip install {" ".join(pip_pkgs)}'
        msg += f'\n  or run setup: "{sys.executable}" "{SCRIPTS_DIR / "setup.py"}"'
        raise RuntimeError(msg)


def speech_locales() -> list[str] | None:
    """SpeechTranscriber's supported locales via `transcribe --locales`
    (1.6.0+ binary). None when the flag is unavailable — an older binary
    treats it as a file path and exits 2 — so callers make no verdict."""
    if not Path(TRANSCRIBE).exists():
        return None
    try:
        proc = subprocess.run([TRANSCRIBE, "--locales"], capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            return None
        data = json.loads(proc.stdout)
        return [str(x) for x in data] if isinstance(data, list) else None
    except Exception:  # noqa: BLE001 — no list, no verdict
        return None


def vision_languages() -> list[str] | None:
    try:
        import ocr as ocr_mod  # lazy: Vision loads only when asked
        return ocr_mod.supported_languages()
    except Exception:  # noqa: BLE001
        return None


def validate_locale(locale: str, speech: list[str] | None, vision: list[str] | None) -> list[str]:
    """Check a (normalised) locale against what the two frameworks support.
    Unsupported by SpeechTranscriber -> ValueError naming the supported set
    (the run would fail minutes later inside the transcriber otherwise).
    Unsupported by Vision -> a warning; OCR falls back to en-US. A None list
    (old transcribe binary, no pyobjc) yields no verdict."""
    warnings: list[str] = []
    if speech is not None and not locale_matches(locale, speech):
        raise ValueError(
            f"--locale {locale} is not supported by SpeechTranscriber. Supported: "
            + ", ".join(sorted(speech))
        )
    if vision is not None and not locale_matches(locale, vision):
        warnings.append(
            f"--locale {locale} is not a Vision OCR language; on-screen text will be "
            f"read as en-US only. Vision supports: {', '.join(sorted(vision))}"
        )
    return warnings


def _validate(args) -> None:
    """Reject out-of-range numerics before they reach arithmetic or ffmpeg
    (e.g. --max-frames 0 is a division by zero in thinning)."""
    checks = [
        (math.isfinite(args.scene) and 0 <= args.scene <= 1,
         f"--scene must be in [0, 1] (got {args.scene})"),
        (args.floor is None or (math.isfinite(args.floor) and args.floor > 0),
         f"--floor must be > 0 (got {args.floor})"),
        (args.width >= 16, f"--width must be >= 16 (got {args.width})"),
        (args.max_frames >= 1, f"--max-frames must be >= 1 (got {args.max_frames})"),
        (math.isfinite(args.threshold) and 0 <= args.threshold <= 1,
         f"--threshold must be in [0, 1] (got {args.threshold})"),
    ]
    for good, msg in checks:
        if not good:
            raise ValueError(msg)


def _params(args) -> dict:
    _validate(args)
    # Normalise once, in place: everything downstream (cache key, captions,
    # OCR, transcriber) sees the same tag, so en_US and en-us cannot fork.
    args.locale = normalize_locale(args.locale)
    start = parse_ts(args.start) if args.start is not None else None
    end = parse_ts(args.end) if args.end is not None else None
    if start is not None and start < 0:
        raise ValueError(f"--start must be >= 0 (got {args.start})")
    if end is not None and end <= (start or 0):
        raise ValueError(
            f"invalid focus window: --end ({args.end}) must be after --start "
            f"({args.start if args.start is not None else '0'})"
        )
    # Cache-key the EFFECTIVE floor: --floor 5 clamps to the 2s cap, so it must
    # share a cache entry with the default rather than re-extracting.
    floor = min(args.floor, frames_mod.FLOOR_CAP) if args.floor else frames_mod.FLOOR_CAP
    return {
        "version": VERSION_TAG,
        "scene": args.scene,
        "floor": floor,
        "width": args.width,
        "max_frames": args.max_frames,
        "locale": args.locale,
        "repull": not args.no_repull,
        "threshold": args.threshold,
        "start": start,
        "end": end,
    }


def _purge_artifacts(ad: Path, full_run: bool) -> None:
    """Drop the extracted artifacts so a --no-cache run can't read stale frames
    or a stale digest. The transcript is re-generated below when forced. A full
    (non-window) hard bypass also drops all focused-window artifacts, since they
    reference the media being re-downloaded."""
    for sub in ("frames", "sheets"):
        if (ad / sub).exists():
            shutil.rmtree(ad / sub, ignore_errors=True)
    for name in ("frames.json", "sheets.json", "ocr.json", "watch.md", "done.json"):
        (ad / name).unlink(missing_ok=True)
    if full_run and (ad / "windows").exists():
        shutil.rmtree(ad / "windows", ignore_errors=True)


def _cache_hit(ad: Path, params: dict) -> bool:
    try:
        done = ad / "done.json"
        if not done.exists():
            return False
        prev = read_json(done)
        if prev.get("version") != params["version"]:
            return False
        if any(prev.get(k) != params[k] for k in CACHE_KEYS):
            return False
        # every artifact the digest references must still exist
        if not (ad / "watch.md").exists():
            return False
        manifest = read_json(ad / "frames.json").get("frames", [])
        if any(not (ad / "frames" / m["file"]).exists() for m in manifest):
            return False  # frames were deleted out from under the digest
        if (ad / "sheets.json").exists():
            sheets = read_json(ad / "sheets.json").get("sheets", [])
            if any(not (ad / s["file"]).exists() for s in sheets):
                return False  # sheets were deleted out from under the digest
        return True
    except Exception:  # noqa: BLE001 — an unreadable cache is a MISS, not an error
        log("cache entry unreadable; re-extracting")
        return False


def _run_concurrently(*fns, stop: threading.Event | None = None) -> None:
    """Run the phases in parallel and re-raise the FIRST failure as soon as it
    happens. f1.result(); f2.result() used to block on a minutes-long OCR pass
    while the transcript had already failed. The survivor is told to stop via
    `stop` (OCR checks it per frame); the executor is not joined here so the
    error reaches the user immediately."""
    stop = stop if stop is not None else threading.Event()
    ex = ThreadPoolExecutor(max_workers=len(fns))
    futs = [ex.submit(fn) for fn in fns]
    try:
        done, _pending = wait(futs, return_when=FIRST_EXCEPTION)
        for f in done:
            exc = f.exception()
            if exc is not None:
                stop.set()
                raise exc
        for f in futs:  # all finished cleanly
            f.result()
    finally:
        ex.shutdown(wait=False)


def _log_cache_size() -> None:
    mb = cache_size_bytes() / 1e6
    size = f"{mb / 1000:.1f} GB" if mb >= 1000 else f"{mb:.0f} MB"
    log(f"cache: {size} at {CACHE_ROOT}")


def run_pipeline(source: str, args) -> str:
    params = _params(args)
    is_url = not Path(source).exists()
    vid = video_id_for(source)
    wd = work_dir(vid)
    ad = artifact_dir(wd, params["start"], params["end"])
    _log_cache_size()

    def cached() -> str:
        log(f"cache hit ({vid}); reusing extracted result")
        if args.summary_only:  # render-only flag: rebuild from the cached JSON
            return assemble_mod.render_cached(wd, ad, summary_only=True)
        return (ad / "watch.md").read_text(encoding="utf-8")

    if not args.no_cache and _cache_hit(ad, params):
        return cached()

    _preflight(is_url)
    for warning in validate_locale(args.locale, speech_locales(), vision_languages()):
        log(f"warn: {warning}")

    with video_lock(wd):
        # Re-check under the lock: if we waited on a concurrent run of the same
        # video, it may have produced exactly the result we need.
        if not args.no_cache and _cache_hit(ad, params):
            return cached()
        return _run_pipeline_locked(source, args, params, is_url, vid, wd, ad)


def _run_pipeline_locked(source: str, args, params: dict, is_url: bool,
                         vid: str, wd: Path, ad: Path) -> str:
    # --no-cache is a HARD bypass: re-download and re-extract, never read any
    # frames/digest left from a previous run.
    if args.no_cache:
        _purge_artifacts(ad, full_run=ad == wd)
        if is_url:
            # The shared media is about to be re-downloaded; every sibling
            # cache entry (full video + other windows) references the old
            # bytes, so none may keep serving hits.
            (wd / "done.json").unlink(missing_ok=True)
            for stale in wd.glob("windows/*/done.json"):
                stale.unlink(missing_ok=True)

    # Phase 1 — must finish first (everything else needs meta.json).
    meta = download_mod.download(source, wd, force=args.no_cache, locale=args.locale)

    duration = float(meta.get("duration") or 0.0)
    if params["start"] is not None and duration and params["start"] >= duration:
        raise ValueError(
            f"--start ({args.start}) is beyond the video's end ({meta.get('duration_hms')})"
        )

    has_video = meta.get("has_video", True)
    stop = threading.Event()  # raised when the sibling phase fails

    # Phases 2+3 (frames -> OCR) run alongside Phase 4 (transcript).
    def frames_then_ocr():
        if not has_video:
            log("no video stream; skipping frames + OCR (audio-only source)")
            frames_mod.write_stub(ad)
            write_json(ad / "ocr.json", {"engine": None, "count": 0, "frames": []})
            return
        import ocr as ocr_mod  # lazy: Vision loads only when frames exist

        frames_mod.extract(
            wd, args.scene, args.floor, args.width, args.max_frames,
            params["start"], params["end"], ad=ad,
            force_times=chapter_starts(meta),  # every chapter start gets a frame
        )
        if stop.is_set():
            return
        ocr_mod.ocr_frames(ad, args.locale, stop=stop)

    def do_transcript():
        # The transcript is window-independent and immutable for a given video,
        # so a focused re-run reuses it instead of re-transcribing the whole
        # clip — but only if it matches the requested locale (captions are
        # locale-independent of the flag).
        if not args.no_cache and (wd / "transcript.json").exists():
            try:
                prev = read_json(wd / "transcript.json")
            except Exception:  # noqa: BLE001 — corrupt file -> re-transcribe
                prev = None
            if prev and prev.get("source") != "error" and (
                prev.get("locale") == args.locale
                or str(prev.get("source", "")).startswith("captions")
            ):
                log("reusing existing transcript")
                return
        transcribe_mod.transcribe(wd, args.locale)

    _run_concurrently(frames_then_ocr, do_transcript, stop=stop)

    # Phase 5 — assemble (+ low-confidence hi-res re-pull).
    digest = assemble_mod.assemble(wd, ad, repull=not args.no_repull,
                                   threshold=args.threshold, locale=args.locale,
                                   summary_only=args.summary_only)

    # Phase 6 — stamp the cache.
    write_json(ad / "done.json", params)
    log(f"done ({vid})")
    return digest


def _forget_url(vid: str, source: str) -> int:
    """Drop every url_ids.json entry for this source/id. The map is a
    plaintext watch history; --purge promises the video is gone, so its URL
    must go too. Returns how many entries were removed."""
    try:
        mapping = read_json(URL_ID_MAP)
    except Exception:  # noqa: BLE001 — no/corrupt map -> nothing to forget
        return 0
    keep = {u: v for u, v in mapping.items() if u != source and v != vid}
    removed = len(mapping) - len(keep)
    if removed:
        write_json(URL_ID_MAP, keep)
    return removed


def purge(source: str) -> None:
    vid = video_id_for(source)
    wd = work_dir(vid, create=False)
    if wd.exists():
        shutil.rmtree(wd, ignore_errors=True)
        log(f"purged cache for {vid} ({wd})")
    else:
        log(f"nothing cached for {vid}")
    if _forget_url(vid, source):
        log("removed the URL from url_ids.json")
    _log_cache_size()


def purge_history() -> None:
    """Delete url_ids.json — the URL -> cache-id history. Cached videos stay;
    their next URL lookup costs one yt-dlp metadata call."""
    if URL_ID_MAP.exists():
        URL_ID_MAP.unlink()
        log(f"cleared URL history ({URL_ID_MAP})")
    else:
        log("no URL history to clear")


def main() -> None:
    ap = argparse.ArgumentParser(description="Watch a video on-device (Apple Silicon).")
    ap.add_argument("source", help="video URL or local file path, or 'doctor' for an environment report")
    ap.add_argument("--scene", type=float, default=0.3, help="scene-cut threshold (0-1)")
    ap.add_argument("--floor", type=float, default=None, help="seconds; sample static shots at least this often (capped at 2s)")
    ap.add_argument("--width", type=int, default=512, help="frame width in px")
    ap.add_argument("--max-frames", type=int, default=300)
    ap.add_argument("--locale", default="en-US")
    ap.add_argument("--start", default=None, help="focus window start (SS, MM:SS, or HH:MM:SS)")
    ap.add_argument("--end", default=None, help="focus window end (SS, MM:SS, or HH:MM:SS)")
    ap.add_argument("--no-repull", action="store_true", help="skip hi-res re-pull of low-confidence frames")
    ap.add_argument("--summary-only", action="store_true",
                    help="print header + transcript + on-screen text only (no frame/sheet paths)")
    ap.add_argument("--threshold", type=float, default=assemble_mod.LOW_CONF)
    ap.add_argument("--no-cache", action="store_true", help="hard bypass: re-download + re-extract, ignore any cache")
    ap.add_argument("--purge", action="store_true",
                    help="delete this video's cache dir (and its url_ids.json entry) and exit")
    ap.add_argument("--purge-history", action="store_true",
                    help="delete url_ids.json, the URL -> cache-id history, and exit")
    args = ap.parse_args()

    # The digest may contain any language; never let a C/POSIX shell locale
    # turn a finished extraction into a UnicodeEncodeError at print time.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    # `watch.py doctor` — environment report, exit 1 on a missing prerequisite.
    # ponytail: a file literally named "doctor" in the cwd is shadowed; pass ./doctor.
    if args.source == "doctor" and not Path("doctor").exists():
        import doctor as doctor_mod
        sys.exit(doctor_mod.exit_code())

    try:
        if args.purge_history:
            purge_history()
            return
        source = resolve_source(args.source)
        if args.purge:
            purge(source)
            return
        digest = run_pipeline(source, args)
    except Exception as e:  # noqa: BLE001 — top-level friendly error
        log(f"ERROR: {e}")
        sys.exit(1)
    print(digest)


if __name__ == "__main__":
    main()
