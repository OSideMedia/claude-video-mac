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
import html
import json
import re
import subprocess
from pathlib import Path

from common import (
    FFMPEG,
    TRANSCRIBE,
    fmt_vtt_ts,
    log,
    read_json,
    resolve_speech_locale,
    run,
    speech_locale_supported,
    video_id_for,
    work_dir,
    write_json,
    write_text_atomic,
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
            text = html.unescape(text)                   # &amp;#39; etc. in auto-captions
            text = re.sub(r"\s+", " ", text).strip()
            if text:
                segments.append({"start": cur_start, "end": cur_end, "text": text})
        block = []
        cur_start = cur_end = None

    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
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


def check_speech_locale(locale: str, supported: list[str] | None) -> None:
    """The hard locale gate, placed where it is actually needed: right before
    on-device transcription. A captioned source in Arabic or Thai never gets
    here, and OCR (Vision) is not limited by SpeechTranscriber's list."""
    if supported is None or speech_locale_supported(locale, supported):
        return
    raise RuntimeError(
        f"--locale {locale} is not supported by SpeechTranscriber (supported: "
        f"{', '.join(sorted(supported))}). This source has no usable captions, so "
        "on-device transcription was required; captions (when a site has them) and "
        "on-screen text OCR are not limited by this list — pick a supported locale "
        "or a captioned source."
    )


def speech_transcribe(video_path: str, wd: Path, locale: str = "en-US",
                      supported_locales: list[str] | None = None, stop=None) -> list[dict]:
    if not Path(TRANSCRIBE).exists():
        raise RuntimeError(
            f"transcribe CLI not built ({TRANSCRIBE}); run setup.py first"
        )
    supported = speech_locales() if supported_locales is None else supported_locales
    # A bare language ('en') reaching here from another entry point is resolved
    # to the full locale the CLI needs — the CLI gets exactly what we checked.
    resolved = resolve_speech_locale(locale, supported)
    if resolved != locale:
        log(f"--locale {locale} -> {resolved} for SpeechTranscriber")
        locale = resolved
    # Refuse BEFORE extracting audio: a wrong locale must not cost a wav pass.
    check_speech_locale(locale, supported)
    wav = wd / "audio_16k.wav"
    log("extracting 16kHz mono audio…")
    run([
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-i", video_path, "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "pcm_s16le", str(wav),
    ], stop=stop)
    log("running on-device SpeechTranscriber…")
    try:
        out = run([TRANSCRIBE, str(wav), locale], stop=stop).stdout
        data = json.loads(out)
    finally:
        # The wav is a pure intermediate (~115 MB/hour) — re-derivable from the
        # retained media, so it must not sit in the cache, least of all after a
        # failed transcription.
        wav.unlink(missing_ok=True)
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
    write_text_atomic(path, "\n".join(lines))


def transcribe(wd: Path, locale: str = "en-US", stop=None) -> dict:
    meta = read_json(wd / "meta.json")
    cap = meta.get("captions_path")

    if cap and Path(cap).exists():
        log(f"using native captions ({meta.get('captions_kind')})")
        segments = parse_vtt(Path(cap))
        source = f"captions:{meta.get('captions_kind')}"
    elif meta.get("has_audio"):
        try:
            segments = speech_transcribe(meta["video_path"], wd, locale, stop=stop)
        except Exception as e:
            # Leave an honest record on disk (the digest renders source=error
            # as "transcription failed: …") before the pipeline aborts.
            write_json(wd / "transcript.json", {
                "source": "error", "locale": locale, "error": str(e)[-500:],
                "segment_count": 0, "segments": [], "text": "",
            })
            raise
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
