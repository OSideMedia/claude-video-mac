"""Phase 5: assemble frames + on-screen-text + transcript into the context Claude
receives, mirroring the original /watch contract.

Also performs the high-res re-pull: any frame whose OCR confidence is low gets
re-extracted at native resolution and re-OCR'd, so Claude sees a sharper image
and a better-confidence text reading for exactly the frames that need it.

stdout is the Claude-ready digest (timestamped transcript + on-screen text +
frame paths tagged t=MM:SS). A copy is saved as watch.md.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import sheets as sheets_mod
from common import (
    FFMPEG,
    artifact_dir,
    fmt_ts,
    fmt_vtt_ts,
    log,
    parse_ts,
    read_json,
    video_id_for,
    work_dir,
    write_json,
)

LOW_CONF = 0.5  # re-pull a frame's text below this confidence


def repull_lowconf(ad: Path, ocr: dict, meta: dict, threshold: float = LOW_CONF) -> int:
    """Re-extract + re-OCR low-confidence frames at native resolution.

    Updates ocr['frames'] entries in place; returns how many were upgraded.
    """
    from ocr import ocr_image  # lazy: keeps Vision out of cache-hit-only runs

    video_path = meta["video_path"]
    hires_dir = ad / "frames" / "hires"
    upgraded = 0

    for fr in ocr["frames"]:
        mc = fr.get("min_confidence")
        if not fr["lines"] or mc is None or mc >= threshold:
            continue
        hires_dir.mkdir(parents=True, exist_ok=True)
        dest = hires_dir / f"hires_{fr['index']:04d}.jpg"
        # Accurate seek to the frame's timestamp, full native resolution.
        try:
            from common import run
            run([
                FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                "-hwaccel", "videotoolbox",
                "-ss", f"{fr['t']:.3f}", "-i", video_path,
                "-frames:v", "1", "-q:v", "2", str(dest),
            ])
        except Exception as e:  # noqa: BLE001
            log(f"re-pull failed at t={fr['t_hms']}: {e}")
            continue

        new_lines = ocr_image(str(dest))
        new_confs = [l["confidence"] for l in new_lines]
        new_min = min(new_confs) if new_confs else 0.0
        if new_min > (mc or 0):
            fr["lines"] = new_lines
            fr["text"] = " ".join(l["text"] for l in new_lines)
            fr["min_confidence"] = round(new_min, 3)
            fr["mean_confidence"] = round(sum(new_confs) / len(new_confs), 3)
            fr["hires_file"] = f"hires/{dest.name}"
            upgraded += 1
            log(f"re-pulled t={fr['t_hms']}: min_conf {mc:.2f} -> {new_min:.2f}")
    return upgraded


def build_digest(ad: Path, meta: dict, frames: dict, ocr: dict, transcript: dict,
                 sheets: dict | None = None) -> str:
    frames_dir = ad / "frames"
    audio_only = not meta.get("has_video", True)
    lines: list[str] = []
    a = lines.append

    a(f"# Video: {meta.get('source')}")
    a("")
    a(f"- duration: {meta.get('duration_hms')} ({meta.get('duration')}s)")
    if audio_only:
        a("- **audio-only source — no visual layer (no frames / on-screen text)**")
    else:
        a(f"- resolution: {meta.get('width')}x{meta.get('height')} @ {meta.get('fps')}fps")
    a(f"- transcript source: {transcript.get('source')}  "
      f"({transcript.get('segment_count')} segments)")
    a(f"- frames sampled: {frames.get('count')}  |  OCR engine: {ocr.get('engine')}")
    win = frames.get("window")
    if win:
        a(f"- **focused window: {fmt_ts(win[0])}–{fmt_ts(win[1])}** "
          f"(frames cover only this range; the transcript is full-length)")
    a("")

    # --- Transcript ---
    a("## Transcript (timestamped)")
    a("")
    if transcript["segments"]:
        for s in transcript["segments"]:
            a(f"[{fmt_vtt_ts(s['start'])} → {fmt_vtt_ts(s['end'])}] {s['text']}")
    else:
        a("_(no transcript: no captions and no audio)_")
    a("")

    # --- On-screen text ---
    a("## On-screen text (OCR, by frame)")
    a("")
    any_text = False
    for fr in ocr["frames"]:
        if fr["lines"]:
            any_text = True
            conf = fr.get("min_confidence")
            joined = " / ".join(l["text"] for l in fr["lines"])
            a(f"t={fr['t_hms']}: {joined}  (min_conf {conf:.2f})")
    if not any_text:
        a("_(no visual layer: audio-only source)_" if audio_only
          else "_(no on-screen text detected)_")
    a("")

    # --- Frames (image paths for the harness to load) ---
    a("## Frames")
    a("")
    if audio_only:
        a("_(no frames: audio-only source)_")
    else:
        if sheets and sheets.get("count"):
            cols, rows = sheets["cols"], sheets["rows"]
            a(f"_Contact sheets first: each tiles up to {cols * rows} consecutive "
              f"frames ({cols}x{rows} grid), timestamp labeled top-left, time running "
              "left-to-right then top-to-bottom. **Read the sheets for the video's "
              "visual structure**, then read individual full-size frames below only "
              "for moments you need to inspect closely (small text, fine detail)._")
            a("")
            for s in sheets["sheets"]:
                a(f"sheet {s['start_hms']}–{s['end_hms']}  {ad / s['file']}")
            a("")
            a("_Individual frames (full size):_")
        else:
            a("_Load these images to see the video. Each is tagged with its timestamp._")
        a("")
        by_index = {f["index"]: f for f in ocr["frames"]}
        for fr in frames["frames"]:
            o = by_index.get(fr["index"], {})
            img = o.get("hires_file") or fr["file"]
            path = frames_dir / img
            note = ""
            if o.get("hires_file"):
                note = "  (hi-res re-pull)"
            a(f"t={fr['t_hms']}  {path}{note}")
    a("")
    return "\n".join(lines)


def assemble(wd: Path, ad: Path | None = None, repull: bool = True,
             threshold: float = LOW_CONF) -> str:
    """`wd` holds meta + transcript (shared); `ad` holds the run's frames/OCR
    artifacts and receives watch.md (same dir for a full-video run)."""
    if ad is None:
        ad = wd
    meta = read_json(wd / "meta.json")
    frames = read_json(ad / "frames.json")
    ocr = read_json(ad / "ocr.json")
    transcript = read_json(wd / "transcript.json")

    if repull and frames.get("count"):
        n = repull_lowconf(ad, ocr, meta, threshold)
        if n:
            write_json(ad / "ocr.json", ocr)  # persist upgrades
        log(f"hi-res re-pull upgraded {n} frame(s)")

    # Contact sheets are an optimization, never a blocker: a render failure
    # falls back to the individual-frames digest.
    sheets = None
    if frames.get("count", 0) >= sheets_mod.MIN_FRAMES:
        try:
            sheets = sheets_mod.build(ad, frames)
        except Exception as e:  # noqa: BLE001
            log(f"contact sheets skipped ({e})")

    digest = build_digest(ad, meta, frames, ocr, transcript, sheets)
    (ad / "watch.md").write_text(digest)
    return digest


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", help="video URL/path (pipeline must have run)")
    ap.add_argument("--no-repull", action="store_true")
    ap.add_argument("--threshold", type=float, default=LOW_CONF)
    ap.add_argument("--start", default=None, help="window start (matches the extraction run)")
    ap.add_argument("--end", default=None, help="window end (matches the extraction run)")
    args = ap.parse_args()
    wd = work_dir(video_id_for(args.source))
    start = parse_ts(args.start) if args.start is not None else None
    end = parse_ts(args.end) if args.end is not None else None
    ad = artifact_dir(wd, start, end)
    digest = assemble(wd, ad, repull=not args.no_repull, threshold=args.threshold)
    print(digest)


if __name__ == "__main__":
    main()
