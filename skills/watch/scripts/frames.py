"""Phase 2: hardware-accelerated, scene-aware frame extraction.

One ffmpeg pass:
  -hwaccel videotoolbox            decode on the Apple Silicon media engine
  select='scene-cut OR time-floor' keep a frame on every scene change AND at
                                   least once per `floor` seconds, so a long
                                   static shot still gets sampled
  scale -> 512px wide JPEGs
  showinfo                         lets us recover each kept frame's true
                                   source timestamp (pts_time)

We sample DENSELY (floor capped at 2s, see FLOOR_CAP) so short-lived on-screen
cards — stat cards, callouts, lower-thirds in tutorials — can't fall between
samples. To keep that from exploding the token cost, near-identical frames are
collapsed afterwards with a perceptual hash (dhash): static talking-head
stretches shrink to a few representatives while every distinct card survives.

Optional --start/--end restrict the pass to a window (focused re-extraction).

Emits frames.json: an ordered list of {index, t, t_hms, file}.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

from common import (
    FFMPEG,
    artifact_dir,
    fmt_ts,
    log,
    parse_ts,
    read_json,
    run,
    video_id_for,
    work_dir,
    write_json,
)

PTS_RE = re.compile(r"pts_time:([0-9.]+)")

# Hard cap on the static-sampling interval. The whole point is catching cards
# that are only on screen for ~3s, so we never let a long runtime loosen this.
FLOOR_CAP = 2.0
# dhash hamming distance at/below which two frames are treated as near-identical
# and the later one is dropped. Measured: genuinely distinct frames score >8,
# near-duplicates score <=6, so 6 collapses static stretches without eating cards.
DEDUP_HAMMING = 6
# Frames whose dhash matches must ALSO be this close in mean absolute gray
# level (0-255, on a LUMA_GRID² thumbnail) to count as duplicates — gradients
# alone can't tell a navy card from a green card with the same layout.
LUMA_GRID = 16
LUMA_DIFF = 10


def adaptive_floor(duration: float) -> float:
    """Sample static content at least this often. Capped at FLOOR_CAP seconds
    REGARDLESS of duration — a long video must not loosen the sampling past the
    point where a short-lived on-screen card would slip between frames."""
    return FLOOR_CAP


def _gray_pixels(cg, w: int, h: int) -> bytes:
    """Downsample a CGImage to w x h grayscale pixels (row-major bytes)."""
    import Quartz

    cs = Quartz.CGColorSpaceCreateDeviceGray()
    ctx = Quartz.CGBitmapContextCreate(None, w, h, 8, w, cs, Quartz.kCGImageAlphaNone)
    Quartz.CGContextSetInterpolationQuality(ctx, Quartz.kCGInterpolationHigh)
    Quartz.CGContextDrawImage(ctx, Quartz.CGRectMake(0, 0, w, h), cg)
    img = Quartz.CGBitmapContextCreateImage(ctx)
    raw = bytes(Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(img)))
    bpr = Quartz.CGImageGetBytesPerRow(img)  # may be padded past `w` for alignment
    return b"".join(raw[r * bpr : r * bpr + w] for r in range(h))


def _frame_sig(path: str, hash_size: int = 8) -> tuple[int, bytes]:
    """Perceptual signature via CoreGraphics (no extra deps; pyobjc Quartz is
    already required for OCR): a difference hash (horizontal gradients on a
    (hash_size+1)xhash_size grayscale downsample) plus a LUMA_GRID² grayscale
    thumbnail. The dhash alone is blind to absolute luminance — two cards with
    the same layout on different background colors hash identically — so the
    thumbnail supplies the absolute-brightness check the gradients can't."""
    import Quartz
    from Foundation import NSURL

    url = NSURL.fileURLWithPath_(path)
    src = Quartz.CGImageSourceCreateWithURL(url, None)
    if src is None:
        raise RuntimeError(f"cannot read image: {path}")
    cg = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)

    w, h = hash_size + 1, hash_size
    g = _gray_pixels(cg, w, h)
    bits = 0
    for r in range(h):
        row = g[r * w : (r + 1) * w]
        for c in range(hash_size):
            bits = (bits << 1) | (1 if row[c] < row[c + 1] else 0)
    return bits, _gray_pixels(cg, LUMA_GRID, LUMA_GRID)


def _is_near_dup(a: tuple[int, bytes], b: tuple[int, bytes], threshold: int) -> bool:
    if bin(a[0] ^ b[0]).count("1") > threshold:
        return False
    diff = sum(abs(x - y) for x, y in zip(a[1], b[1])) / len(a[1])
    return diff <= LUMA_DIFF


def _dedup_perceptual(pairs: list, threshold: int = DEDUP_HAMMING) -> list:
    """Drop near-identical frames, comparing each to the last *kept* frame. The
    first occurrence of any distinct visual (e.g. a card appearing) is always
    kept. Best-effort: if hashing is unavailable, return the input untouched."""
    if len(pairs) < 2:
        return pairs
    try:
        kept = [pairs[0]]
        last_sig = _frame_sig(str(pairs[0][0]))
        for f, t in pairs[1:]:
            try:
                sig = _frame_sig(str(f))
            except Exception:  # noqa: BLE001 — a bad frame shouldn't drop coverage
                kept.append((f, t))
                continue
            if not _is_near_dup(sig, last_sig, threshold):
                kept.append((f, t))
                last_sig = sig
        return kept
    except Exception as e:  # noqa: BLE001 — never let dedup break extraction
        log(f"perceptual dedup skipped ({e}); keeping all frames")
        return pairs


def extract(
    wd: Path,
    scene_threshold: float = 0.3,
    floor: float | None = None,
    width: int = 512,
    max_frames: int = 300,
    start: float | None = None,
    end: float | None = None,
    ad: Path | None = None,
) -> dict:
    """`wd` holds the source + meta; artifacts (frames/, frames.json) go to
    `ad` — the same dir for a full-video run, a windows/<span> subdir for a
    focused run, so focused passes never clobber the full-video cache."""
    meta = read_json(wd / "meta.json")
    video_path = meta["video_path"]
    duration = float(meta.get("duration") or 0.0)
    if ad is None:
        ad = artifact_dir(wd, start, end)
    if floor is None:
        floor = adaptive_floor(duration)
    elif floor > FLOOR_CAP:
        # SKILL contract: the floor is capped at FLOOR_CAP so a sparse pass can
        # never poison the cache past the density short-lived cards need.
        log(f"--floor {floor:g}s exceeds the {FLOOR_CAP:g}s cap; clamping")
        floor = FLOOR_CAP

    # Window handling: input-seek to `start`, read `end-start` seconds. showinfo
    # then reports output-relative timestamps starting at ~0, so we add `start`
    # back to recover true source time.
    offset = float(start) if start is not None else 0.0
    span = (float(end) - offset) if end is not None else (duration - offset if duration else None)

    frames_dir = ad / "frames"
    frames_dir.mkdir(exist_ok=True)
    for old in frames_dir.glob("frame_*.jpg"):
        old.unlink()

    # select fires when: first frame, OR a scene cut, OR `floor` seconds have
    # elapsed since the previously *selected* frame (prev_selected_t).
    sel = (
        f"select='eq(n\\,0)+gt(scene\\,{scene_threshold})"
        f"+gte(t-prev_selected_t\\,{floor})'"
    )
    # only downscale (never upscale): min(width, iw); -2 keeps height even
    scale = f"scale='min({width}\\,iw)':-2"
    vf = f"{sel},{scale},showinfo"

    cmd = [FFMPEG, "-hide_banner", "-y", "-hwaccel", "videotoolbox"]
    if start is not None:
        cmd += ["-ss", f"{offset:.3f}"]
    if span is not None and span > 0:
        cmd += ["-t", f"{span:.3f}"]
    cmd += [
        "-i", video_path,
        "-vf", vf,
        "-fps_mode", "passthrough",   # keep exactly the selected frames
        "-q:v", "3",
        str(frames_dir / "frame_%04d.jpg"),
    ]

    win = f", window {fmt_ts(offset)}–{fmt_ts(offset + span)}" if span else ""
    log(f"extracting frames (scene>{scene_threshold}, floor={floor:.1f}s, {width}px{win})…")
    proc = run(cmd)

    # showinfo prints one pts_time per kept frame, in output order. Add the
    # window offset so a focused pass still carries true source timestamps.
    times = [float(m) + offset for m in PTS_RE.findall(proc.stderr)]
    files = sorted(frames_dir.glob("frame_*.jpg"))
    if len(times) != len(files):
        # showinfo lines vs files can desync if ffmpeg logs oddly; fall back to
        # an even time grid so we never emit a frame with a wrong timestamp.
        log(f"warn: {len(times)} timestamps vs {len(files)} files; using grid")
        n = len(files)
        grid_span = span if span else duration
        times = [offset + grid_span * i / max(1, n) for i in range(n)]

    pairs = list(zip(files, times))

    # Dense capture, cheap output: collapse near-identical frames before they
    # reach OCR/Claude. Distinct cards survive; static stretches shrink.
    before = len(pairs)
    pairs = _dedup_perceptual(pairs)
    dropped = before - len(pairs)
    if dropped:
        log(f"perceptual dedup: {before} -> {len(pairs)} frames ({dropped} near-dup dropped)")
        # remove the JPEGs we just dropped so they don't linger in frames/
        keep_files = {f for f, _ in pairs}
        for f, _ in zip(files, times):
            if f not in keep_files and f.exists():
                f.unlink()

    # Safety cap: if scene cuts produced too many frames, keep an even subset.
    if len(pairs) > max_frames:
        step = len(pairs) / max_frames
        keep = {int(i * step) for i in range(max_frames)}
        log(f"thinning {len(pairs)} -> {max_frames} frames")
        pairs = [p for i, p in enumerate(pairs) if i in keep]

    # Rename to carry the timestamp, build the manifest.
    manifest = []
    for idx, (f, t) in enumerate(pairs):
        hms = fmt_ts(t)
        safe = hms.replace(":", "m", 1).replace(":", "") + "s"  # 00:12 -> 00m12s
        dest = frames_dir / f"frame_{idx:04d}_t{safe}.jpg"
        f.rename(dest)
        manifest.append({"index": idx, "t": round(t, 3), "t_hms": hms, "file": dest.name})

    # Remove any frames we dropped during thinning.
    kept = {m["file"] for m in manifest}
    for f in frames_dir.glob("frame_*.jpg"):
        if f.name not in kept and re.match(r"frame_\d{4}\.jpg", f.name):
            f.unlink()

    out = {
        "scene_threshold": scene_threshold,
        "floor": round(floor, 3),
        "width": width,
        "window": ([round(offset, 3), round(offset + span, 3)] if span else None),
        "deduped_from": before,
        "count": len(manifest),
        "frames": manifest,
    }
    write_json(ad / "frames.json", out)
    log(f"kept {len(manifest)} frames")
    return out


def write_stub(ad: Path, reason: str = "audio-only source") -> dict:
    """Empty frames manifest for sources with no video stream, so downstream
    phases (OCR, assemble) keep their contract without special-casing."""
    out = {"scene_threshold": None, "floor": None, "width": None,
           "window": None, "deduped_from": 0, "count": 0, "frames": [],
           "note": reason}
    write_json(ad / "frames.json", out)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", help="video URL or local path (must be probed already)")
    ap.add_argument("--scene", type=float, default=0.3)
    ap.add_argument("--floor", type=float, default=None)
    ap.add_argument("--width", type=int, default=512)
    ap.add_argument("--max-frames", type=int, default=300)
    ap.add_argument("--start", default=None, help="window start (SS, MM:SS, or HH:MM:SS)")
    ap.add_argument("--end", default=None, help="window end (SS, MM:SS, or HH:MM:SS)")
    args = ap.parse_args()
    wd = work_dir(video_id_for(args.source))
    start = parse_ts(args.start) if args.start is not None else None
    end = parse_ts(args.end) if args.end is not None else None
    out = extract(wd, args.scene, args.floor, args.width, args.max_frames, start, end)
    for m in out["frames"]:
        print(f"  t={m['t_hms']}  {m['file']}")
    print(f"[{out['count']} frames]")


if __name__ == "__main__":
    main()
