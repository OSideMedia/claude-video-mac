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
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
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
    VERSION_TAG,
    artifact_dir,
    cache_size_bytes,
    log,
    parse_ts,
    read_json,
    video_id_for,
    work_dir,
    write_json,
)

# `start`/`end` are part of the key: a focused-window run must never be served a
# digest computed from a different (e.g. full-video, sparser) window, and vice versa.
CACHE_KEYS = ("scene", "floor", "width", "max_frames", "locale", "repull", "threshold", "start", "end")


def _preflight() -> None:
    """Friendly failures instead of tracebacks when setup.py hasn't run."""
    problems = []
    for name, path in (("ffmpeg", FFMPEG), ("ffprobe", FFPROBE)):
        if not Path(path).exists() and shutil.which(name) is None:
            problems.append(f"{name} not found")
    for mod in ("Vision", "Quartz"):
        if importlib.util.find_spec(mod) is None:
            problems.append(f"pyobjc-framework-{mod} not installed")
    if problems:
        raise RuntimeError(
            "missing components: " + ", ".join(problems)
            + f'\n  run setup first:  python3 "{SCRIPTS_DIR / "setup.py"}"'
        )


def _params(args) -> dict:
    start = parse_ts(args.start) if args.start is not None else None
    end = parse_ts(args.end) if args.end is not None else None
    if start is not None and start < 0:
        raise ValueError(f"--start must be >= 0 (got {args.start})")
    if start is not None and end is not None and end <= start:
        raise ValueError(
            f"invalid focus window: --end ({args.end}) must be after --start ({args.start})"
        )
    return {
        "version": VERSION_TAG,
        "scene": args.scene,
        "floor": args.floor,
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
    frames_dir = ad / "frames"
    if frames_dir.exists():
        shutil.rmtree(frames_dir, ignore_errors=True)
    for name in ("frames.json", "ocr.json", "watch.md", "done.json"):
        (ad / name).unlink(missing_ok=True)
    if full_run and (ad / "windows").exists():
        shutil.rmtree(ad / "windows", ignore_errors=True)


def _cache_hit(ad: Path, params: dict) -> bool:
    done = ad / "done.json"
    if not done.exists():
        return False
    prev = read_json(done)
    if prev.get("version") != params["version"]:
        return False
    if any(prev.get(k) != params[k] for k in CACHE_KEYS):
        return False
    # the artifacts the digest references must still exist
    if not ((ad / "watch.md").exists() and (ad / "frames.json").exists()):
        return False
    manifest = read_json(ad / "frames.json").get("frames", [])
    if manifest and not (ad / "frames" / manifest[0]["file"]).exists():
        return False  # frames were deleted out from under the digest
    return True


def _log_cache_size() -> None:
    mb = cache_size_bytes() / 1e6
    size = f"{mb / 1000:.1f} GB" if mb >= 1000 else f"{mb:.0f} MB"
    log(f"cache: {size} at {CACHE_ROOT}")


def run_pipeline(source: str, args) -> str:
    params = _params(args)
    vid = video_id_for(source)
    wd = work_dir(vid)
    ad = artifact_dir(wd, params["start"], params["end"])
    _log_cache_size()

    if not args.no_cache and _cache_hit(ad, params):
        log(f"cache hit ({vid}); reusing extracted result")
        return (ad / "watch.md").read_text()

    _preflight()

    # --no-cache is a HARD bypass: re-download and re-extract, never read any
    # frames/digest left from a previous run.
    if args.no_cache:
        _purge_artifacts(ad, full_run=ad == wd)

    # Phase 1 — must finish first (everything else needs meta.json).
    meta = download_mod.download(source, wd, force=args.no_cache)

    duration = float(meta.get("duration") or 0.0)
    if params["start"] is not None and duration and params["start"] >= duration:
        raise ValueError(
            f"--start ({args.start}) is beyond the video's end ({meta.get('duration_hms')})"
        )

    has_video = meta.get("has_video", True)

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
        )
        ocr_mod.ocr_frames(ad, args.locale)

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
            if prev and (
                prev.get("locale") == args.locale
                or str(prev.get("source", "")).startswith("captions")
            ):
                log("reusing existing transcript")
                return
        transcribe_mod.transcribe(wd, args.locale)

    with ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(frames_then_ocr)
        f2 = ex.submit(do_transcript)
        f1.result()
        f2.result()

    # Phase 5 — assemble (+ low-confidence hi-res re-pull).
    digest = assemble_mod.assemble(wd, ad, repull=not args.no_repull, threshold=args.threshold)

    # Phase 6 — stamp the cache.
    write_json(ad / "done.json", params)
    log(f"done ({vid})")
    return digest


def purge(source: str) -> None:
    vid = video_id_for(source)
    wd = work_dir(vid, create=False)
    if wd.exists():
        shutil.rmtree(wd, ignore_errors=True)
        log(f"purged cache for {vid} ({wd})")
    else:
        log(f"nothing cached for {vid}")
    _log_cache_size()


def main() -> None:
    ap = argparse.ArgumentParser(description="Watch a video on-device (Apple Silicon).")
    ap.add_argument("source", help="video URL or local file path")
    ap.add_argument("--scene", type=float, default=0.3, help="scene-cut threshold (0-1)")
    ap.add_argument("--floor", type=float, default=None, help="seconds; sample static shots at least this often (capped at 2s)")
    ap.add_argument("--width", type=int, default=512, help="frame width in px")
    ap.add_argument("--max-frames", type=int, default=300)
    ap.add_argument("--locale", default="en-US")
    ap.add_argument("--start", default=None, help="focus window start (SS, MM:SS, or HH:MM:SS)")
    ap.add_argument("--end", default=None, help="focus window end (SS, MM:SS, or HH:MM:SS)")
    ap.add_argument("--no-repull", action="store_true", help="skip hi-res re-pull of low-confidence frames")
    ap.add_argument("--threshold", type=float, default=assemble_mod.LOW_CONF)
    ap.add_argument("--no-cache", action="store_true", help="hard bypass: re-download + re-extract, ignore any cache")
    ap.add_argument("--purge", action="store_true", help="delete this video's cache dir and exit")
    args = ap.parse_args()

    try:
        if args.purge:
            purge(args.source)
            return
        digest = run_pipeline(args.source, args)
    except Exception as e:  # noqa: BLE001 — top-level friendly error
        log(f"ERROR: {e}")
        sys.exit(1)
    print(digest)


if __name__ == "__main__":
    main()
