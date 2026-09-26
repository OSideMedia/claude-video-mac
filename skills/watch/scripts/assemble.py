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
    write_text_atomic,
)

LOW_CONF = 0.5  # re-pull a frame's text below this confidence

# Transcript display: merge consecutive cues into readable paragraphs (auto-
# captions emit a cue every ~1-2s; one digest line per cue triples token cost).
MERGE_GAP = 3.0    # seconds of silence that starts a new paragraph
MERGE_SPAN = 25.0  # max seconds one merged paragraph may cover
# Focused runs show only the window's transcript (± this context margin).
WINDOW_MARGIN = 10.0


def repull_lowconf(ad: Path, ocr: dict, meta: dict, threshold: float = LOW_CONF,
                   locale: str = "en-US") -> int:
    """Re-extract + re-OCR low-confidence frames at native resolution.

    Updates ocr['frames'] entries in place; returns how many were upgraded.
    """
    from ocr import ocr_image, recognition_languages  # lazy: keeps Vision out of cache-hit-only runs

    video_path = meta["video_path"]
    hires_dir = ad / "frames" / "hires"
    upgraded = 0
    # Same language list as the first OCR pass — an en-US-only re-pull would
    # silently drop non-English text on exactly the frames that needed help.
    languages = tuple(recognition_languages(locale))

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

        new_lines = ocr_image(str(dest), languages=languages)
        new_confs = [l["confidence"] for l in new_lines]
        new_min = min(new_confs) if new_confs else 0.0
        # A "better" reading must not LOSE text: one crisp line at 0.9 beating
        # five lines whose weakest was 0.4 would drop four lines of content.
        if new_min > (mc or 0) and len(new_lines) >= len(fr["lines"]):
            fr["lines"] = new_lines
            fr["text"] = " ".join(l["text"] for l in new_lines)
            fr["min_confidence"] = round(new_min, 3)
            fr["mean_confidence"] = round(sum(new_confs) / len(new_confs), 3)
            fr["hires_file"] = f"hires/{dest.name}"
            upgraded += 1
            log(f"re-pulled t={fr['t_hms']}: min_conf {mc:.2f} -> {new_min:.2f}")
    return upgraded


def merge_segments(segments: list[dict]) -> list[dict]:
    """Coalesce consecutive cues into paragraphs of <= MERGE_SPAN seconds,
    breaking at gaps > MERGE_GAP. Display-only — the raw segments stay in
    transcript.json/.vtt."""
    merged: list[dict] = []
    for s in segments:
        if (merged
                and s["start"] - merged[-1]["end"] <= MERGE_GAP
                and s["end"] - merged[-1]["start"] <= MERGE_SPAN):
            merged[-1]["end"] = s["end"]
            merged[-1]["text"] += " " + s["text"]
        else:
            merged.append(dict(s))
    return merged


def empty_transcript_note(transcript: dict) -> str:
    """Say WHY there is no transcript. The header two lines up names the
    source, so 'no captions and no audio' under 'speechtranscriber (0
    segments)' was a contradiction — that case is silence, not absence."""
    source = str(transcript.get("source") or "none")
    if source == "speechtranscriber":
        return ("_(no speech detected: on-device transcription ran over the audio "
                "track and found no words)_")
    if source.startswith("captions"):
        return "_(no transcript: the native caption track was empty)_"
    if source == "error":
        return f"_(transcription failed: {transcript.get('error') or 'unknown error'})_"
    return "_(no transcript: no captions and no audio track)_"


def build_digest(ad: Path, meta: dict, frames: dict, ocr: dict, transcript: dict,
                 sheets: dict | None = None, wd: Path | None = None,
                 summary_only: bool = False) -> str:
    """`summary_only`: header + transcript + on-screen text, no frame/sheet
    paths — for questions the text layers answer without looking."""
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
    ocr_err = ocr.get("errors") or 0
    err_note = f"  |  **{ocr_err} frame(s) failed OCR** (undecodable image; listed without text)" if ocr_err else ""
    a(f"- frames sampled: {frames.get('count')}  |  OCR engine: {ocr.get('engine')}{err_note}")
    max_gap = frames.get("max_gap")
    if frames.get("count") and max_gap is not None:
        thin_note = " — sampling was THINNED by --max-frames" if frames.get("thinned") else ""
        a(f"- frame coverage: largest gap between frames {max_gap:.1f}s{thin_note}")
        if frames.get("thinned"):
            a("  (do not assert something is absent from a gap this size; "
            "use a --start/--end focused re-run instead)")
    if frames.get("timestamps_estimated"):
        a("- **frame timestamps are ESTIMATED** (ffmpeg's per-frame timing desynced; "
          "frames were placed on an even grid — treat every t= below as approximate)")
    win = frames.get("window")
    win_end_s = None
    if win:
        win_end_s = win[1]
        win_label = f"{fmt_ts(win[0])}–{fmt_ts(win[1]) if win[1] is not None else 'end'}"
        a(f"- **focused window: {win_label}** (frames and the transcript below "
          f"cover only this range)")
    a("")

    # --- Site metadata (URL sources; came free with the id lookup) ---
    if any(meta.get(k) for k in ("title", "uploader", "upload_date")):
        a("## Video")
        a("")
        if meta.get("title"):
            a(f"- title: {meta['title']}")
        if meta.get("uploader"):
            a(f"- uploader: {meta['uploader']}")
        if meta.get("upload_date"):
            a(f"- date: {meta['upload_date']}")
        a(f"- duration: {meta.get('duration_hms')}")
        a("")
    if meta.get("chapters"):
        a("## Chapters")
        a("")
        for c in meta["chapters"]:
            a(f"{fmt_ts(c['start'])}  {c.get('title') or '(untitled)'}")
        a("")

    # --- Transcript ---
    a("## Transcript (timestamped)")
    a("")
    segments = merge_segments(transcript["segments"])
    if win:
        lo = win[0] - WINDOW_MARGIN
        hi = (win_end_s + WINDOW_MARGIN) if win_end_s is not None else float("inf")
        shown = [s for s in segments if s["end"] >= lo and s["start"] <= hi]
        vtt = (wd / "transcript.vtt") if wd else None
        a(f"_Showing the window's transcript only ({len(shown)} of "
          f"{len(segments)} paragraphs). Full transcript: {vtt}_" if vtt else
          f"_Showing the window's transcript only ({len(shown)} of {len(segments)} paragraphs)._")
        a("")
        segments = shown
    if segments:
        for s in segments:
            a(f"[{fmt_vtt_ts(s['start'])} → {fmt_vtt_ts(s['end'])}] {s['text']}")
    elif transcript["segments"]:
        a("_(no speech within the focused window)_")
    else:
        a(empty_transcript_note(transcript))
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

    if summary_only:
        a("_(summary-only digest: frame and contact-sheet paths omitted; re-run "
          "without --summary-only to see the video)_")
        a("")
        return "\n".join(lines)

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
        # The directory once, then basenames: ~300 frame lines each repeating
        # a ~90-char absolute path was most of the digest's token cost.
        a(f"frames dir: {frames_dir}")
        a("(join the dir and a basename below to read a frame)")
        a("")
        by_index = {f["index"]: f for f in ocr["frames"]}
        for fr in frames["frames"]:
            o = by_index.get(fr["index"], {})
            img = o.get("hires_file") or fr["file"]
            note = "  (hi-res re-pull)" if o.get("hires_file") else ""
            if fr.get("chapter"):
                note += "  (chapter start)"
            a(f"t={fr['t_hms']}  {img}{note}")
    a("")
    return "\n".join(lines)


def render_cached(wd: Path, ad: Path, summary_only: bool = False) -> str:
    """Re-render the digest from the JSON a finished run left behind (no
    re-pull, no sheet rebuild). Lets render-only flags like --summary-only
    serve a cache hit without re-extracting."""
    meta = read_json(wd / "meta.json")
    frames = read_json(ad / "frames.json")
    ocr = read_json(ad / "ocr.json")
    transcript = read_json(wd / "transcript.json")
    sheets = read_json(ad / "sheets.json") if (ad / "sheets.json").exists() else None
    return build_digest(ad, meta, frames, ocr, transcript, sheets, wd=wd,
                        summary_only=summary_only)


def assemble(wd: Path, ad: Path | None = None, repull: bool = True,
             threshold: float = LOW_CONF, locale: str = "en-US",
             summary_only: bool = False) -> str:
    """`wd` holds meta + transcript (shared); `ad` holds the run's frames/OCR
    artifacts and receives watch.md (same dir for a full-video run). watch.md
    is always the FULL digest so the cache stays complete; `summary_only`
    only changes what this call returns."""
    if ad is None:
        ad = wd
    meta = read_json(wd / "meta.json")
    frames = read_json(ad / "frames.json")
    ocr = read_json(ad / "ocr.json")
    transcript = read_json(wd / "transcript.json")

    if repull and frames.get("count"):
        n = repull_lowconf(ad, ocr, meta, threshold, locale)
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

    digest = build_digest(ad, meta, frames, ocr, transcript, sheets, wd=wd)
    write_text_atomic(ad / "watch.md", digest)  # a killed run must not leave a half digest
    if summary_only:
        return build_digest(ad, meta, frames, ocr, transcript, sheets, wd=wd, summary_only=True)
    return digest


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", help="video URL/path (pipeline must have run)")
    ap.add_argument("--no-repull", action="store_true")
    ap.add_argument("--threshold", type=float, default=LOW_CONF)
    ap.add_argument("--locale", default="en-US")
    ap.add_argument("--start", default=None, help="window start (matches the extraction run)")
    ap.add_argument("--end", default=None, help="window end (matches the extraction run)")
    ap.add_argument("--summary-only", action="store_true")
    args = ap.parse_args()
    wd = work_dir(video_id_for(args.source))
    start = parse_ts(args.start) if args.start is not None else None
    end = parse_ts(args.end) if args.end is not None else None
    ad = artifact_dir(wd, start, end)
    digest = assemble(wd, ad, repull=not args.no_repull, threshold=args.threshold,
                      locale=args.locale, summary_only=args.summary_only)
    print(digest)


if __name__ == "__main__":
    main()
