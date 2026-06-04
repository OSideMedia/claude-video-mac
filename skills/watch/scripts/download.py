"""Phase 1: resolve a source to a local video file + metadata.

URL  -> yt-dlp download (capped resolution) + native captions if the host has them.
Local -> probe in place, no copy.

Either way we emit meta.json describing the clip so later phases never re-probe.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from common import (
    FFPROBE,
    YTDLP,
    fmt_ts,
    log,
    run,
    video_id_for,
    work_dir,
    write_json,
)


def probe(video_path: Path) -> dict:
    """ffprobe -> duration, dims, fps, has_audio."""
    out = run(
        [
            FFPROBE, "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", str(video_path),
        ]
    ).stdout
    data = json.loads(out)
    vstream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    astream = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)

    # avg_frame_rate is "30000/1001"; reduce to a float, guard divide-by-zero.
    fps = 0.0
    afr = vstream.get("avg_frame_rate", "0/0")
    if "/" in afr:
        num, den = afr.split("/")
        fps = round(float(num) / float(den), 3) if float(den) else 0.0

    duration = float(data.get("format", {}).get("duration") or vstream.get("duration") or 0.0)
    return {
        "duration": round(duration, 3),
        "duration_hms": fmt_ts(duration),
        "width": int(vstream.get("width") or 0),
        "height": int(vstream.get("height") or 0),
        "fps": fps,
        "video_codec": vstream.get("codec_name"),
        "has_audio": astream is not None,
        "audio_codec": astream.get("codec_name") if astream else None,
    }


def _fetch_captions(source: str, wd: Path, out_tmpl: str) -> tuple[Path | None, str | None]:
    """Best-effort English captions. Prefer manual; fall back to auto-generated.

    Run as two separate, non-fatal passes so a 429 on one track (or no track at
    all) never aborts the pipeline. Restrict to plain English variants — no
    wildcard, which would otherwise pull machine-translated tracks like en-de.
    """
    base = [*YTDLP.split(), "--no-warnings", "--no-playlist", "--skip-download",
            "--convert-subs", "vtt", "--sub-langs", "en,en-US,en-orig",
            "-o", out_tmpl, source]
    # Pass 1: manual subtitles only.
    try:
        run([*base, "--write-subs"])
    except Exception as e:  # noqa: BLE001 — best effort
        log(f"manual-caption fetch skipped: {str(e).splitlines()[-1][:120]}")
    manual = sorted(wd.glob("source*.vtt"))
    if manual:
        return manual[0], "manual"
    # Pass 2: auto-generated (ASR) subtitles.
    try:
        run([*base, "--write-auto-subs"])
    except Exception as e:  # noqa: BLE001 — best effort
        log(f"auto-caption fetch skipped: {str(e).splitlines()[-1][:120]}")
    auto = sorted(wd.glob("source*.vtt"))
    if auto:
        return auto[0], "auto"
    return None, None


def download(source: str, wd: Path, force: bool = False) -> dict:
    src_path = Path(source)
    if src_path.exists():
        log(f"local source: {src_path}")
        video_path = src_path.resolve()
        captions, cap_kind = None, None
    else:
        # --no-cache hard bypass: drop any previously downloaded media/captions
        # so yt-dlp genuinely re-fetches instead of reporting "already downloaded".
        if force:
            for old in wd.glob("source.*"):
                old.unlink(missing_ok=True)
            log("forced re-download (--no-cache)")
        log(f"downloading: {source}")
        out_tmpl = str(wd / "source.%(ext)s")
        # Video download — must succeed.
        run([
            *YTDLP.split(),
            "--no-warnings", "--no-playlist",
            "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
            "--merge-output-format", "mp4",
            "-o", out_tmpl,
            source,
        ])
        vids = [p for p in wd.glob("source.*") if p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}]
        if not vids:
            raise RuntimeError("yt-dlp produced no video file")
        video_path = vids[0].resolve()

        # Captions — best effort. A 429 or a missing track must never sink the
        # pipeline; we just fall back to on-device transcription.
        captions, cap_kind = _fetch_captions(source, wd, out_tmpl)
        if captions:
            log(f"captions found ({cap_kind}): {captions.name}")
        else:
            log("no usable native captions; transcript will come from SpeechTranscriber")

    info = probe(video_path)
    meta = {
        "source": source,
        "video_path": str(video_path),
        "captions_path": str(captions) if captions else None,
        "captions_kind": cap_kind,
        **info,
    }
    write_json(wd / "meta.json", meta)
    log(f"probed: {info['duration_hms']}  {info['width']}x{info['height']}  "
        f"{info['fps']}fps  audio={info['has_audio']}")
    return meta


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", help="video URL or local file path")
    args = ap.parse_args()
    vid = video_id_for(args.source)
    wd = work_dir(vid)
    meta = download(args.source, wd)
    print(json.dumps(meta, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
