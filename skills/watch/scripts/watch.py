"""watch.py — entry point orchestrating the Mac-native /watch pipeline.

    python3 watch.py <url-or-path> [options]

Phases: download/probe -> (frames -> OCR) ‖ transcript -> assemble.
Frames+OCR and the transcript are independent, so they run concurrently.

Cache (Phase 6): results are keyed by video id under ~/.cache/claude-video-mac/.
A completed run drops done.json recording the parameters; a later invocation
with matching parameters reprints the cached digest without re-extracting.
"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import assemble as assemble_mod
import download as download_mod
import frames as frames_mod
import ocr as ocr_mod
import transcribe as transcribe_mod
from common import VERSION_TAG, log, read_json, video_id_for, work_dir, write_json

CACHE_KEYS = ("scene", "floor", "width", "max_frames", "locale", "repull", "threshold")


def _params(args) -> dict:
    return {
        "version": VERSION_TAG,
        "scene": args.scene,
        "floor": args.floor,
        "width": args.width,
        "max_frames": args.max_frames,
        "locale": args.locale,
        "repull": not args.no_repull,
        "threshold": args.threshold,
    }


def _cache_hit(wd: Path, params: dict) -> bool:
    done = wd / "done.json"
    if not done.exists():
        return False
    prev = read_json(done)
    if prev.get("version") != params["version"]:
        return False
    if any(prev.get(k) != params[k] for k in CACHE_KEYS):
        return False
    # the artifacts the digest references must still exist
    return (wd / "watch.md").exists() and (wd / "frames.json").exists()


def run_pipeline(source: str, args) -> str:
    vid = video_id_for(source)
    wd = work_dir(vid)
    params = _params(args)

    if not args.no_cache and _cache_hit(wd, params):
        log(f"cache hit ({vid}); reusing extracted result")
        return (wd / "watch.md").read_text()

    # Phase 1 — must finish first (everything else needs meta.json).
    download_mod.download(source, wd)

    # Phases 2+3 (frames -> OCR) run alongside Phase 4 (transcript).
    def frames_then_ocr():
        frames_mod.extract(wd, args.scene, args.floor, args.width, args.max_frames)
        ocr_mod.ocr_frames(wd)

    def do_transcript():
        transcribe_mod.transcribe(wd, args.locale)

    with ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(frames_then_ocr)
        f2 = ex.submit(do_transcript)
        f1.result()
        f2.result()

    # Phase 5 — assemble (+ low-confidence hi-res re-pull).
    digest = assemble_mod.assemble(wd, repull=not args.no_repull, threshold=args.threshold)

    # Phase 6 — stamp the cache.
    write_json(wd / "done.json", params)
    log(f"done ({vid})")
    return digest


def main() -> None:
    ap = argparse.ArgumentParser(description="Watch a video on-device (Apple Silicon).")
    ap.add_argument("source", help="video URL or local file path")
    ap.add_argument("--scene", type=float, default=0.3, help="scene-cut threshold (0-1)")
    ap.add_argument("--floor", type=float, default=None, help="seconds; sample static shots at least this often")
    ap.add_argument("--width", type=int, default=512, help="frame width in px")
    ap.add_argument("--max-frames", type=int, default=300)
    ap.add_argument("--locale", default="en-US")
    ap.add_argument("--no-repull", action="store_true", help="skip hi-res re-pull of low-confidence frames")
    ap.add_argument("--threshold", type=float, default=assemble_mod.LOW_CONF)
    ap.add_argument("--no-cache", action="store_true", help="force re-extraction")
    args = ap.parse_args()

    try:
        digest = run_pipeline(args.source, args)
    except Exception as e:  # noqa: BLE001 — top-level friendly error
        log(f"ERROR: {e}")
        sys.exit(1)
    print(digest)


if __name__ == "__main__":
    main()
