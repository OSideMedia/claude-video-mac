"""Phase 1: resolve a source to a local video file + metadata.

URL  -> yt-dlp download (capped resolution) + native captions if the host has them.
Local -> probe in place, no copy.

Either way we emit meta.json describing the clip so later phases never re-probe.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from common import (
    FFMPEG,
    FFPROBE,
    MEDIA_EXTS,
    YTDLP,
    YTDLP_COMMON,
    fmt_ts,
    log,
    read_json,
    run,
    video_id_for,
    work_dir,
    write_json,
)


def _ytdlp_base() -> list[str]:
    """yt-dlp argv prefix. Points yt-dlp at our resolved ffmpeg so DASH
    format merging and --convert-subs work on machines with no system ffmpeg."""
    base = [*YTDLP]
    if Path(FFMPEG).exists():
        base += ["--ffmpeg-location", str(Path(FFMPEG).parent)]
    return base


def _is_cover_art(stream: dict) -> bool:
    """ffprobe reports embedded album art (podcast .m4a/.mp3) as a video stream
    with disposition.attached_pic=1; treating it as the video would run
    frames/OCR/sheets over a 64x64 JPEG."""
    return bool((stream.get("disposition") or {}).get("attached_pic"))


def probe_info(data: dict) -> dict:
    """Pure part of probe(): parsed ffprobe JSON -> the meta fields."""
    streams = data.get("streams", [])
    vstream = next((s for s in streams
                    if s.get("codec_type") == "video" and not _is_cover_art(s)), {})
    astream = next((s for s in streams if s.get("codec_type") == "audio"), None)

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
        "has_video": bool(vstream),
        "video_codec": vstream.get("codec_name"),
        "has_audio": astream is not None,
        "audio_codec": astream.get("codec_name") if astream else None,
    }


def probe(video_path: Path) -> dict:
    """ffprobe -> duration, dims, fps, has_audio."""
    out = run(
        [
            FFPROBE, "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", str(video_path),
        ]
    ).stdout
    return probe_info(json.loads(out))


def _caption_langs(locale: str) -> str:
    """Track list for --sub-langs, requested language FIRST. Plain named
    variants only — no wildcard, which would otherwise pull machine-translated
    tracks like en-de. A non-en locale asks for its own tracks first, keeping
    English as the fallback; pick_caption() then ranks whatever arrived."""
    en = "en,en-US,en-orig"
    if locale.lower().startswith("en"):
        return en
    lang = locale.split("-")[0]
    return f"{locale},{lang},{en}"


def caption_lang_tag(path: Path) -> str | None:
    """source.en-US.vtt -> 'en-US'; source.vtt -> None."""
    parts = path.name.split(".")
    return parts[1] if len(parts) >= 3 and parts[0] == "source" else None


def _norm_tag(tag: str) -> str:
    return tag.replace("_", "-").lower()


def caption_rank(path: Path, locale: str) -> int:
    """0 exact locale, 1 same language, 2 an English fallback, 3 anything else.
    Lower is better."""
    tag = caption_lang_tag(path)
    if tag is None:
        return 3
    tag, loc = _norm_tag(tag), _norm_tag(locale)
    if tag == loc:
        return 0
    if tag.split("-")[0] == loc.split("-")[0]:
        return 1
    if tag.split("-")[0] == "en":
        return 2
    return 3


def caption_matches(path: Path, locale: str) -> bool:
    """Same language as the requested locale (exact or language-only tag)."""
    return caption_rank(path, locale) <= 1


def pick_caption(paths: list[Path], locale: str) -> Path | None:
    """Best track for the locale; ties broken by name so the choice is stable.
    Previously sorted(glob)[0] picked alphabetically, so a fr-FR request that
    fetched source.en.vtt + source.fr.vtt got English (it/ja/ko/pt/ru/zh too)."""
    if not paths:
        return None
    return min(paths, key=lambda p: (caption_rank(p, locale), p.name))


def captions_argv(source: str, out_tmpl: str, locale: str, flag: str) -> list[str]:
    """yt-dlp argv for one caption pass (flag: --write-subs / --write-auto-subs)."""
    return [*_ytdlp_base(), *YTDLP_COMMON, "--skip-download",
            "--convert-subs", "vtt", "--sub-langs", _caption_langs(locale),
            flag, "-o", out_tmpl, "--", source]


def media_argv(source: str, out_tmpl: str) -> list[str]:
    """yt-dlp argv for the media download. Final /ba branch: audio-only
    sources (podcasts, no-video streams) have no video stream to request."""
    return [*_ytdlp_base(), *YTDLP_COMMON,
            "-f", "bv*[height<=1080]+ba/b[height<=1080]/b/ba",
            "--merge-output-format", "mp4",
            "-o", out_tmpl, "--", source]


def _fetch_captions(source: str, wd: Path, out_tmpl: str,
                    locale: str = "en-US") -> tuple[Path | None, str | None]:
    """Best-effort captions. Prefer manual; fall back to auto-generated.

    Run as two separate, non-fatal passes so a 429 on one track (or no track at
    all) never aborts the pipeline. Tracks left by a previous run are reused
    only when one matches the requested language, or when the same locale was
    requested before (so its fallback outcome is already known) — never just
    because SOME .vtt exists.
    """
    meta: dict = {}
    try:
        meta = read_json(wd / "meta.json")
    except Exception:  # noqa: BLE001 — no/corrupt meta -> kind unknown
        pass
    existing = sorted(wd.glob("source*.vtt"))
    prior_kind = meta.get("captions_kind") or "unknown"
    if existing:
        best = pick_caption(existing, locale)
        if caption_matches(best, locale) or meta.get("captions_locale") == locale:
            return best, prior_kind

    # ponytail: one call per pass downloads the fallback tracks too (a few KB
    # each); split into requested-then-fallback calls if bandwidth matters.
    before = set(existing)
    for flag, kind in (("--write-subs", "manual"), ("--write-auto-subs", "auto")):
        try:
            run(captions_argv(source, out_tmpl, locale, flag))
        except Exception as e:  # noqa: BLE001 — best effort
            log(f"{kind}-caption fetch skipped: {str(e).splitlines()[-1][:120]}")
        new = [p for p in sorted(wd.glob("source*.vtt")) if p not in before]
        if new:
            return pick_caption(new, locale), kind
    if existing:  # nothing new: an older fallback track beats no transcript
        return pick_caption(existing, locale), prior_kind
    return None, None


def existing_media(wd: Path) -> Path | None:
    """The finished download, if any: exact "source.<ext>" (fragments are
    source.fNNN.<ext>) with a supported media extension — an audio-only result
    is a valid outcome."""
    media = sorted(p for p in wd.glob("source.*")
                   if p.suffix.lower() in MEDIA_EXTS and p.stem == "source")
    return media[0] if media else None


def needs_download(wd: Path, force: bool) -> bool:
    """yt-dlp used to run on EVERY cache miss — including --start/--end
    re-runs with source.* already on disk. Only --force (--no-cache) re-fetches."""
    return force or existing_media(wd) is None


def download(source: str, wd: Path, force: bool = False, locale: str = "en-US") -> dict:
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
        # Leftover DASH fragments from an interrupted merge (source.f401.mp4)
        # must not be mistaken for the finished file below. Numeric format ids
        # only — source.fr.vtt is a caption file, not a fragment.
        for frag in wd.glob("source.f*.*"):
            if re.match(r"source\.f\d+\.", frag.name):
                frag.unlink(missing_ok=True)
        out_tmpl = str(wd / "source.%(ext)s")
        if needs_download(wd, force):
            log(f"downloading: {source}")
            run(media_argv(source, out_tmpl))  # media download — must succeed
        else:
            log("media already downloaded; reusing it")
        media = existing_media(wd)
        if media is None:
            raise RuntimeError("yt-dlp produced no media file")
        video_path = media.resolve()

        # Captions — best effort. A 429 or a missing track must never sink the
        # pipeline; we just fall back to on-device transcription.
        captions, cap_kind = _fetch_captions(source, wd, out_tmpl, locale)
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
        "captions_locale": locale,
        **info,
    }
    write_json(wd / "meta.json", meta)
    log(f"probed: {info['duration_hms']}  {info['width']}x{info['height']}  "
        f"{info['fps']}fps  audio={info['has_audio']}")
    return meta


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", help="video URL or local file path")
    ap.add_argument("--locale", default="en-US")
    args = ap.parse_args()
    vid = video_id_for(args.source)
    wd = work_dir(vid)
    meta = download(args.source, wd, locale=args.locale)
    print(json.dumps(meta, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
