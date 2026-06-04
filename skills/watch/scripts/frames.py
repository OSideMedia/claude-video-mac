"""Phase 2: hardware-accelerated, scene-aware frame extraction.

One ffmpeg pass:
  -hwaccel videotoolbox            decode on the Apple Silicon media engine
  select='scene-cut OR time-floor' keep a frame on every scene change AND at
                                   least once per `floor` seconds, so a long
                                   static shot still gets sampled
  scale -> 512px wide JPEGs
  showinfo                         lets us recover each kept frame's true
                                   source timestamp (pts_time)

Emits frames.json: an ordered list of {index, t, t_hms, file}.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

from common import FFMPEG, fmt_ts, log, read_json, run, video_id_for, work_dir, write_json

PTS_RE = re.compile(r"pts_time:([0-9.]+)")


def adaptive_floor(duration: float) -> float:
    """Sample static content at least this often. Scales with clip length so a
    short clip stays dense and an hour-long one doesn't explode the frame count."""
    return max(2.0, duration / 60.0)


def extract(
    wd: Path,
    scene_threshold: float = 0.3,
    floor: float | None = None,
    width: int = 512,
    max_frames: int = 300,
) -> dict:
    meta = read_json(wd / "meta.json")
    video_path = meta["video_path"]
    duration = float(meta.get("duration") or 0.0)
    if floor is None:
        floor = adaptive_floor(duration)

    frames_dir = wd / "frames"
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

    log(f"extracting frames (scene>{scene_threshold}, floor={floor:.1f}s, {width}px)…")
    proc = run([
        FFMPEG, "-hide_banner", "-y",
        "-hwaccel", "videotoolbox",
        "-i", video_path,
        "-vf", vf,
        "-fps_mode", "passthrough",   # keep exactly the selected frames
        "-q:v", "3",
        str(frames_dir / "frame_%04d.jpg"),
    ])

    # showinfo prints one pts_time per kept frame, in output order.
    times = [float(m) for m in PTS_RE.findall(proc.stderr)]
    files = sorted(frames_dir.glob("frame_*.jpg"))
    if len(times) != len(files):
        # showinfo lines vs files can desync if ffmpeg logs oddly; fall back to
        # an even time grid so we never emit a frame with a wrong timestamp.
        log(f"warn: {len(times)} timestamps vs {len(files)} files; using grid")
        n = len(files)
        times = [duration * i / max(1, n) for i in range(n)]

    pairs = list(zip(files, times))

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
        "count": len(manifest),
        "frames": manifest,
    }
    write_json(wd / "frames.json", out)
    log(f"kept {len(manifest)} frames")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", help="video URL or local path (must be probed already)")
    ap.add_argument("--scene", type=float, default=0.3)
    ap.add_argument("--floor", type=float, default=None)
    ap.add_argument("--width", type=int, default=512)
    ap.add_argument("--max-frames", type=int, default=300)
    args = ap.parse_args()
    wd = work_dir(video_id_for(args.source))
    out = extract(wd, args.scene, args.floor, args.width, args.max_frames)
    for m in out["frames"]:
        print(f"  t={m['t_hms']}  {m['file']}")
    print(f"[{out['count']} frames]")


if __name__ == "__main__":
    main()
