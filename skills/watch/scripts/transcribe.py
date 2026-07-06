"""Phase 4: produce a timestamped transcript.

Decision tree:
  1. If the source had native captions (from Phase 1) -> parse that VTT.
  2. Else if the clip has audio -> extract 16kHz mono wav and run the on-device
     Swift SpeechTranscriber CLI.
  3. Else -> empty transcript.

Always writes transcript.json (segments + full text) and transcript.vtt, so the
output mirrors the original /watch skill's VTT contract.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from common import (
    FFMPEG,
    TRANSCRIBE,
    fmt_vtt_ts,
    log,
    read_json,
    run,
    video_id_for,
    work_dir,
    write_json,
)

VTT_CUE_RE = re.compile(
    r"(\d{2}:\d{2}:\d{2}[.,]\d{3}|\d{2}:\d{2}[.,]\d{3})\s*-->\s*"
    r"(\d{2}:\d{2}:\d{2}[.,]\d{3}|\d{2}:\d{2}[.,]\d{3})"
)


def _ts_to_seconds(ts: str) -> float:
    ts = ts.replace(",", ".")
    parts = ts.split(":")
    parts = [float(p) for p in parts]
    while len(parts) < 3:
        parts.insert(0, 0.0)
    h, m, s = parts
    return h * 3600 + m * 60 + s


def parse_vtt(path: Path) -> list[dict]:
    """Minimal WebVTT -> segments. Strips inline tags and de-dupes the rolling
    repetition common in auto-captions."""
    segments: list[dict] = []
    block: list[str] = []
    cur_start = cur_end = None

    def flush():
        nonlocal block, cur_start, cur_end
        if cur_start is not None and block:
            text = " ".join(block).strip()
            text = re.sub(r"<[^>]+>", "", text)          # inline timing tags
            text = re.sub(r"\s+", " ", text).strip()
            if text:
                segments.append({"start": cur_start, "end": cur_end, "text": text})
        block = []
        cur_start = cur_end = None

    for raw in path.read_text(errors="ignore").splitlines():
        line = raw.strip()
        m = VTT_CUE_RE.search(line)
        if m:
            flush()
            cur_start = _ts_to_seconds(m.group(1))
            cur_end = _ts_to_seconds(m.group(2))
        elif not line:
            flush()
        elif line in ("WEBVTT",) or line.startswith(("Kind:", "Language:", "NOTE")):
            continue
        elif cur_start is not None:
            block.append(line)
    flush()

    # Collapse consecutive duplicate lines (auto-caption roll-up artifact).
    deduped: list[dict] = []
    for seg in segments:
        if deduped and seg["text"] == deduped[-1]["text"]:
            deduped[-1]["end"] = seg["end"]
        else:
            deduped.append(seg)
    return deduped


def speech_transcribe(video_path: str, wd: Path, locale: str = "en-US") -> list[dict]:
    if not Path(TRANSCRIBE).exists():
        raise RuntimeError(
            f"transcribe CLI not built ({TRANSCRIBE}); run setup.py first"
        )
    wav = wd / "audio_16k.wav"
    log("extracting 16kHz mono audio…")
    run([
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-i", video_path, "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "pcm_s16le", str(wav),
    ])
    log("running on-device SpeechTranscriber…")
    out = run([TRANSCRIBE, str(wav), locale]).stdout
    data = json.loads(out)
    return [
        {"start": round(s["start"], 3), "end": round(s["end"], 3), "text": s["text"]}
        for s in data.get("segments", [])
    ]


def write_vtt(segments: list[dict], path: Path) -> None:
    lines = ["WEBVTT", ""]
    for i, s in enumerate(segments, 1):
        lines.append(str(i))
        lines.append(f"{fmt_vtt_ts(s['start'])} --> {fmt_vtt_ts(s['end'])}")
        lines.append(s["text"])
        lines.append("")
    path.write_text("\n".join(lines))


def transcribe(wd: Path, locale: str = "en-US") -> dict:
    meta = read_json(wd / "meta.json")
    cap = meta.get("captions_path")

    if cap and Path(cap).exists():
        log(f"using native captions ({meta.get('captions_kind')})")
        segments = parse_vtt(Path(cap))
        source = f"captions:{meta.get('captions_kind')}"
    elif meta.get("has_audio"):
        segments = speech_transcribe(meta["video_path"], wd, locale)
        source = "speechtranscriber"
    else:
        log("no captions and no audio track; empty transcript")
        segments, source = [], "none"

    result = {
        "source": source,
        "locale": locale,
        "segment_count": len(segments),
        "segments": segments,
        "text": " ".join(s["text"] for s in segments),
    }
    write_json(wd / "transcript.json", result)
    write_vtt(segments, wd / "transcript.vtt")
    log(f"transcript: {len(segments)} segments via {source}")
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", help="video URL/path (must be probed already)")
    ap.add_argument("--locale", default="en-US")
    args = ap.parse_args()
    wd = work_dir(video_id_for(args.source))
    res = transcribe(wd, args.locale)
    print(f"[{res['source']}] {res['segment_count']} segments")
    for s in res["segments"][:20]:
        print(f"  {fmt_vtt_ts(s['start'])} {s['text']}")


if __name__ == "__main__":
    main()
