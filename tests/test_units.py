#!/usr/bin/env python3
"""Unit tests for the pure helpers — no media, no setup.py, no network.

Two runners, one file:
  python3 tests/test_units.py            # script runner (fleet registry calls it this way)
  python3 -m pytest tests/test_units.py  # pytest collects every test_* function

Checks that need a macOS framework (Vision/Quartz) skip cleanly where pyobjc is
absent, so the same file runs on the Linux CI matrix.
"""
from __future__ import annotations

import inspect
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "watch" / "scripts"))

# Isolate the modules from the machine: the cache root and the shared bin dir
# are read at import time, so pin them to throwaway dirs BEFORE importing.
_ISOLATED = Path(tempfile.mkdtemp(prefix="watch-units-"))
os.environ.setdefault("WATCH_CACHE_DIR", str(_ISOLATED / "cache"))
os.environ.setdefault("WATCH_BIN_DIR", str(_ISOLATED / "bin"))

import common  # noqa: E402
import frames  # noqa: E402
import transcribe  # noqa: E402
from assemble import merge_segments  # noqa: E402

try:  # pytest is optional: the script runner works without it
    import pytest
except ImportError:  # pragma: no cover
    pytest = None

FAILURES: list[str] = []
_SECTION_START = 0


class _Skip(Exception):
    """Raised by skip() under the script runner (pytest has its own)."""


def check(desc: str, cond: bool) -> bool:
    print(("  ✓ " if cond else "  ✗ ") + desc)
    if not cond:
        FAILURES.append(desc)
    return bool(cond)


def raises(desc: str, fn, exc=Exception) -> bool:
    try:
        fn()
    except exc:
        return check(desc, True)
    except Exception as e:  # noqa: BLE001
        return check(f"{desc} (raised {type(e).__name__} instead)", False)
    return check(desc, False)


def skip(reason: str):
    print(f"  - skipped: {reason}")
    if pytest is not None:
        pytest.skip(reason)
    raise _Skip(reason)


def section(name: str) -> None:
    global _SECTION_START
    _SECTION_START = len(FAILURES)
    print(f"== {name} ==")


def done() -> None:
    """Every test_* function ends with this so pytest fails the function when
    any check in it failed, while the script runner keeps going."""
    bad = FAILURES[_SECTION_START:]
    assert not bad, f"{len(bad)} check(s) failed: {bad}"


def tmpdir() -> Path:
    return Path(tempfile.mkdtemp(dir=_ISOLATED))


# --- timestamps -------------------------------------------------------------
def test_timestamps():
    section("timestamps")
    check("parse_ts '90' -> 90", common.parse_ts("90") == 90.0)
    check("parse_ts '1:30' -> 90", common.parse_ts("1:30") == 90.0)
    check("parse_ts '1:02:03' -> 3723", common.parse_ts("1:02:03") == 3723.0)
    check("parse_ts '12.5' -> 12.5", common.parse_ts("12.5") == 12.5)
    raises("parse_ts rejects 4-part", lambda: common.parse_ts("1:2:3:4"), ValueError)
    check("fmt_ts 75 -> 01:15", common.fmt_ts(75) == "01:15")
    check("fmt_ts 3723 -> 1:02:03", common.fmt_ts(3723) == "1:02:03")
    check("fmt_ts clamps negatives", common.fmt_ts(-3) == "00:00")
    check("fmt_vtt_ts 61.5 -> 00:01:01.500", common.fmt_vtt_ts(61.5) == "00:01:01.500")
    done()


# --- frames: pts regex + thinning ------------------------------------------
def test_frames_helpers():
    section("frames")
    stderr = "pts_time:0.0 x\npts_time:-0.033 x\npts_time:1.5e+01 x\npts_time:2.5 x"
    got = [float(m) for m in frames.PTS_RE.findall(stderr)]
    check("PTS_RE catches negative + scientific pts", got == [0.0, -0.033, 15.0, 2.5])

    pairs = [(f"f{i}", float(i)) for i in range(500)]
    thinned = frames.thin(pairs, 300)
    check("thin caps at max_frames", len(thinned) == 300)
    check("thin keeps the first frame", thinned[0] == pairs[0])
    check("thin keeps the LAST frame", thinned[-1] == pairs[-1])
    check("thin no-ops when under cap", frames.thin(pairs[:10], 300) == pairs[:10])
    check("thin max_frames=1 keeps one", frames.thin(pairs, 1) == [pairs[0]])

    nums = ["frame_000999.jpg", "frame_001000.jpg", "frame_010000.jpg", "frame_009999.jpg"]
    srt = sorted(nums, key=lambda n: int(frames.FRAME_NUM_RE.search(n).group(1)))
    check("numeric frame sort survives width overflow",
          srt == ["frame_000999.jpg", "frame_001000.jpg", "frame_009999.jpg", "frame_010000.jpg"])
    done()


# --- vtt parsing ------------------------------------------------------------
def test_vtt_parsing():
    section("vtt")
    vtt_path = tmpdir() / "cues.vtt"
    vtt_path.write_text(
        "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n<c>it&#39;s</c> a &quot;test&quot;\n\n"
        "00:00:02.000 --> 00:00:03.000\nit's a \"test\"\n\n"
        "00:00:03.000 --> 00:00:04.000\nnext line\n", encoding="utf-8")
    segs = transcribe.parse_vtt(vtt_path)
    check("parse_vtt strips tags + unescapes entities", segs[0]["text"] == 'it\'s a "test"')
    check("parse_vtt collapses rolled-up duplicates", len(segs) == 2 and segs[0]["end"] == 3.0)
    done()


# --- transcript paragraph merging -------------------------------------------
def test_merge_segments():
    section("merge_segments")
    cues = [{"start": float(i), "end": i + 1.0, "text": f"w{i}"} for i in range(10)]
    merged = merge_segments(cues)
    check("adjacent cues merge into one paragraph", len(merged) == 1)
    check("merged span covers all cues", merged[0]["start"] == 0.0 and merged[0]["end"] == 10.0)
    gap = [{"start": 0.0, "end": 1.0, "text": "a"}, {"start": 30.0, "end": 31.0, "text": "b"}]
    check("a large gap starts a new paragraph", len(merge_segments(gap)) == 2)
    long = [{"start": float(i * 2), "end": i * 2 + 2.0, "text": "x"} for i in range(60)]
    check("paragraphs respect the max span",
          all(m["end"] - m["start"] <= 26 for m in merge_segments(long)))
    check("input segments are not mutated", cues[0]["end"] == 1.0)
    done()


# --- cache identity + artifact dirs -----------------------------------------
def test_cache_layout():
    section("cache")
    wd = tmpdir()
    check("artifact_dir full run = wd", common.artifact_dir(wd, None, None) == wd)
    w = common.artifact_dir(wd, 3.0, 6.0)
    check("artifact_dir window is namespaced", w == wd / "windows" / "3.00-6.00")
    check("artifact_dir start-only window", common.artifact_dir(wd, 3.0, None).name == "3.00-end")

    # atomic write_json must not leave partial files behind
    common.write_json(wd / "x.json", {"a": 1})
    check("write_json round-trips", common.read_json(wd / "x.json") == {"a": 1})
    check("write_json leaves no tmp file", not (wd / "x.json.tmp").exists())
    done()


# --- version consistency ----------------------------------------------------
def test_version_consistency():
    section("version consistency")
    plugin = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text())
    market = json.loads((REPO / ".claude-plugin" / "marketplace.json").read_text())
    readme = (REPO / "README.md").read_text()
    changelog = (REPO / "CHANGELOG.md").read_text()
    v = plugin["version"]
    check(f"plugin.json == common.VERSION_TAG ({v})", v == common.VERSION_TAG)
    check("marketplace.json metadata version matches", market["metadata"]["version"] == v)
    check("marketplace.json plugin version matches", market["plugins"][0]["version"] == v)
    check("README badge matches", f"version-{v}-blue" in readme)
    check("CHANGELOG has an entry for it", f"## {v}" in changelog)
    done()


# --- parse_ts rejects non-finite / negative components (item 11) -----------
def test_parse_ts_rejects_garbage():
    section("parse_ts strictness")
    for bad in ("nan", "inf", "-inf", "-5", "1:-30", "1e5", "0x10", "", " ", "1::3", "a:b"):
        raises(f"parse_ts rejects {bad!r}", lambda b=bad: common.parse_ts(b), ValueError)
    raises("parse_ts rejects float nan", lambda: common.parse_ts(float("nan")), ValueError)
    raises("parse_ts rejects float inf", lambda: common.parse_ts(float("inf")), ValueError)
    raises("parse_ts rejects negative number", lambda: common.parse_ts(-1), ValueError)
    check("parse_ts still accepts '00:00:00.500'", common.parse_ts("00:00:00.500") == 0.5)
    check("parse_ts still accepts 0", common.parse_ts(0) == 0.0)
    done()


# --- probe: embedded cover art is not a video stream (item 1) --------------
def test_probe_ignores_cover_art():
    section("probe attached_pic")
    import download
    podcast = {
        "format": {"duration": "3601.5"},
        "streams": [
            {"codec_type": "video", "codec_name": "mjpeg", "width": 64, "height": 64,
             "avg_frame_rate": "0/0", "disposition": {"attached_pic": 1}},
            {"codec_type": "audio", "codec_name": "aac"},
        ],
    }
    info = download.probe_info(podcast)
    check("cover art does not make the file a video", info["has_video"] is False)
    check("cover art dims are not reported", info["width"] == 0 and info["height"] == 0)
    check("audio is still detected", info["has_audio"] is True and info["audio_codec"] == "aac")
    check("duration comes from the container", info["duration"] == 3601.5)
    real = {
        "format": {"duration": "12"},
        "streams": [
            {"codec_type": "video", "codec_name": "mjpeg", "width": 64, "height": 64,
             "avg_frame_rate": "0/0", "disposition": {"attached_pic": 1}},
            {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080,
             "avg_frame_rate": "30000/1001", "disposition": {"attached_pic": 0}},
        ],
    }
    info = download.probe_info(real)
    check("a real video stream after cover art is picked", info["width"] == 1920 and info["fps"] == 29.97)
    done()


# --- digest: why the transcript is empty (item 2) --------------------------
def _digest_inputs(**transcript):
    from assemble import build_digest
    meta = {"source": "x.mp4", "duration": 5.0, "duration_hms": "00:05", "has_video": True,
            "width": 640, "height": 360, "fps": 30.0}
    fr = {"count": 0, "frames": [], "max_gap": 0.0, "thinned": False, "window": None}
    ocr = {"engine": "apple-vision", "count": 0, "frames": []}
    t = {"source": "none", "segment_count": 0, "segments": [], "text": ""}
    t.update(transcript)
    return build_digest(tmpdir(), meta, fr, ocr, t)


def test_digest_empty_transcript_reasons():
    section("digest empty-transcript reasons")
    d = _digest_inputs(source="speechtranscriber")
    check("ASR with 0 segments says 'no speech detected'", "no speech detected" in d)
    check("...and does not claim 'no audio'", "no audio" not in d)
    d = _digest_inputs(source="none")
    check("no audio stream says 'no audio'", "no audio" in d)
    d = _digest_inputs(source="error", error="transcribe CLI exploded")
    check("a transcription error is reported", "transcription failed" in d and "exploded" in d)
    d = _digest_inputs(source="captions:auto")
    check("empty caption track says so", "caption track" in d and "empty" in d)
    done()


# --- yt-dlp argv: '--' before the source, one playlist entry (items 6, 10) --
class _Stub:
    """Records argv of every run() call; replies per a callback."""

    def __init__(self, reply):
        self.calls: list[list[str]] = []
        self.reply = reply

    def __call__(self, cmd, **kw):
        import subprocess
        self.calls.append(list(cmd))
        out = self.reply(cmd)
        if isinstance(out, Exception):
            raise out
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")


def test_ytdlp_id_argv():
    section("yt-dlp id argv")
    url = "https://www.youtube.com/watch?v=abc123&list=PLxyz"
    stub = _Stub(lambda cmd: "Youtube.abc123\n")
    orig = common.run
    common.run = stub
    try:
        vid = common.video_id_for(url)
    finally:
        common.run = orig
    check("id resolved from yt-dlp", vid == "url_Youtube_abc123")
    check("exactly one yt-dlp call", len(stub.calls) == 1)
    argv = stub.calls[0] if stub.calls else []
    check("'--' guards the source", argv[-2:] == ["--", url])
    check("--playlist-items 1 caps a list URL to one entry",
          "--playlist-items" in argv and argv[argv.index("--playlist-items") + 1] == "1")
    # persisted: the second lookup must not touch yt-dlp
    stub2 = _Stub(lambda cmd: RuntimeError("network must not be used"))
    common.run = stub2
    try:
        again = common.video_id_for(url)
    finally:
        common.run = orig
    check("second lookup served from url_ids.json", again == vid and not stub2.calls)
    done()


def test_bare_playlist_refused():
    section("bare playlist URLs")
    bare = ("https://www.youtube.com/playlist?list=PL123",
            "https://www.youtube.com/watch?list=PL123",
            "https://youtube.com/playlist?list=PL123&si=xyz")
    for u in bare:
        check(f"bare playlist detected: {u}", common.is_bare_playlist_url(u))
        raises(f"resolve_source refuses {u}", lambda u=u: common.resolve_source(u), ValueError)
    fine = ("https://www.youtube.com/watch?v=abc&list=PL123",
            "https://youtu.be/abc?list=PL123",
            "https://vimeo.com/123456",
            "https://example.com/video?playlist=1")
    for u in fine:
        check(f"not a bare playlist: {u}", not common.is_bare_playlist_url(u))
        check(f"resolve_source passes {u} through", common.resolve_source(u) == u)
    try:
        common.resolve_source(bare[0])
    except ValueError as e:
        check("refusal names the fix", "v=" in str(e) or "single video" in str(e).lower())
    done()


# --- captions: pick the requested language, not the alphabetical first (item 3)
def _vtt(wd: Path, tag: str) -> Path:
    p = wd / f"source.{tag}.vtt"
    p.write_text("WEBVTT\n", encoding="utf-8")
    return p


def test_caption_selection():
    section("caption language selection")
    import download
    wd = tmpdir()
    en, fr = _vtt(wd, "en"), _vtt(wd, "fr")
    check("pick_caption fr-FR -> source.fr.vtt", download.pick_caption([en, fr], "fr-FR") == fr)
    check("pick_caption en-US -> source.en.vtt", download.pick_caption([en, fr], "en-US") == en)
    ja, en_us, en_orig = _vtt(wd, "ja"), _vtt(wd, "en-US"), _vtt(wd, "en-orig")
    check("exact tag beats language-only", download.pick_caption([en, en_us], "en-US") == en_us)
    check("language-only beats English fallback", download.pick_caption([en, ja], "ja-JP") == ja)
    check("English is the fallback for an absent language",
          download.pick_caption([en, en_orig], "de-DE") in (en, en_orig))
    check("nothing -> None", download.pick_caption([], "fr-FR") is None)
    check("caption_lang_tag reads the tag", download.caption_lang_tag(Path("source.en-US.vtt")) == "en-US")
    check("caption_lang_tag: no tag", download.caption_lang_tag(Path("source.vtt")) is None)
    done()


def test_fetch_captions_honours_locale():
    section("_fetch_captions locale")
    import download
    orig = download.run
    # 1) both tracks already on disk: fr-FR must get the French one
    wd = tmpdir()
    en, fr = _vtt(wd, "en"), _vtt(wd, "fr")
    common.write_json(wd / "meta.json", {"captions_kind": "manual", "captions_locale": "fr-FR"})
    stub = _Stub(lambda cmd: RuntimeError("no network in unit tests"))
    download.run = stub
    try:
        got, kind = download._fetch_captions("https://x/v", wd, str(wd / "source.%(ext)s"), "fr-FR")
    finally:
        download.run = orig
    check("existing fr track chosen for fr-FR", got == fr)
    check("kind from meta", kind == "manual")
    check("no fetch when a matching track exists", not stub.calls)
    # 2) only an English leftover, locale fr-FR never requested before: refetch
    wd = tmpdir()
    en = _vtt(wd, "en")
    common.write_json(wd / "meta.json", {"captions_kind": "auto", "captions_locale": "en-US"})
    stub = _Stub(lambda cmd: "")  # fetch "succeeds" but yields nothing new
    download.run = stub
    try:
        got, kind = download._fetch_captions("https://x/v", wd, str(wd / "source.%(ext)s"), "fr-FR")
    finally:
        download.run = orig
    check("a non-matching leftover triggers a fetch", len(stub.calls) >= 1)
    check("fetch argv requests fr-FR first",
          bool(stub.calls) and stub.calls[0][stub.calls[0].index("--sub-langs") + 1].startswith("fr-FR,fr,"))
    check("fetch argv guards the source with '--'", bool(stub.calls) and stub.calls[0][-2:] == ["--", "https://x/v"])
    check("falls back to the English leftover when nothing new arrives", got == en and kind == "auto")
    # 3) same locale replayed with only English on disk: that is the known outcome, reuse it
    wd = tmpdir()
    en = _vtt(wd, "en")
    common.write_json(wd / "meta.json", {"captions_kind": "auto", "captions_locale": "fr-FR"})
    stub = _Stub(lambda cmd: RuntimeError("no network"))
    download.run = stub
    try:
        got, kind = download._fetch_captions("https://x/v", wd, str(wd / "source.%(ext)s"), "fr-FR")
    finally:
        download.run = orig
    check("same-locale replay reuses the fallback without fetching", got == en and not stub.calls)
    done()


# --- download: reuse media already on disk (item 4) + media argv (items 6, 10)
_FFPROBE_JSON = json.dumps({
    "format": {"duration": "12.0"},
    "streams": [{"codec_type": "video", "codec_name": "h264", "width": 640, "height": 360,
                 "avg_frame_rate": "30/1", "disposition": {"attached_pic": 0}},
                {"codec_type": "audio", "codec_name": "aac"}],
})


def _download_stub(record_media_to: list):
    import download

    def reply(cmd):
        if cmd[0] == download.FFPROBE:
            return _FFPROBE_JSON
        if "--skip-download" in cmd:
            return ""  # caption fetch: nothing new
        record_media_to.append(cmd)
        return RuntimeError("yt-dlp must not run in unit tests")
    return _Stub(reply)


def test_download_skips_when_media_exists():
    section("download reuse")
    import download
    wd = tmpdir()
    (wd / "source.mp4").write_bytes(b"\x00" * 16)
    _vtt(wd, "en")
    common.write_json(wd / "meta.json", {"captions_kind": "manual", "captions_locale": "en-US"})
    media_calls: list = []
    orig = download.run
    download.run = _download_stub(media_calls)
    try:
        meta = download.download("https://x/v", wd, force=False, locale="en-US")
    except Exception as e:  # noqa: BLE001
        meta = {"error": str(e)}
    finally:
        download.run = orig
    check("no yt-dlp media call when source.mp4 exists", not media_calls)
    check("meta points at the existing media", meta.get("video_path") == str((wd / "source.mp4").resolve()))
    check("meta records the caption locale", meta.get("captions_locale") == "en-US")
    # --force (--no-cache) still re-downloads, with the hardened argv
    media_calls.clear()
    download.run = _download_stub(media_calls)
    try:
        download.download("https://x/v", wd, force=True, locale="en-US")
    except Exception:  # noqa: BLE001 — the stub refuses; we only inspect argv
        pass
    finally:
        download.run = orig
    check("--force re-downloads", len(media_calls) == 1)
    argv = media_calls[0] if media_calls else []
    check("media argv guards the source with '--'", argv[-2:] == ["--", "https://x/v"])
    check("media argv caps playlists to one entry", "--playlist-items" in argv)
    done()


# --- frame filenames past 1h (item 8) --------------------------------------
def test_frame_tag_past_one_hour():
    section("frame name tags")
    check("00:12 -> 00m12s (unchanged below 1h)", frames.frame_tag("00:12") == "00m12s")
    check("59:59 -> 59m59s", frames.frame_tag("59:59") == "59m59s")
    check("1:02:03 -> 01h02m03s (was 1m0203s, read as 1m02s)", frames.frame_tag("1:02:03") == "01h02m03s")
    check("10:00:00 -> 10h00m00s", frames.frame_tag("10:00:00") == "10h00m00s")
    check("parse_frame_tag round-trips 1h", frames.parse_frame_tag("frame_0003_t01h02m03s.jpg") == 3723.0)
    check("parse_frame_tag round-trips <1h", frames.parse_frame_tag("frame_0000_t00m12s.jpg") == 12.0)
    check("parse_frame_tag on a raw ffmpeg name -> None", frames.parse_frame_tag("frame_000001.jpg") is None)
    done()


# --- contact-sheet geometry stays under the vision cap (item 9) ------------
def test_sheet_geometry_cap():
    section("sheet geometry")
    import sheets
    cw, ch, cols, rows = sheets.sheet_geometry(512, 288)  # landscape 16:9
    check("landscape grid is 3x4", (cols, rows) == (3, 4))
    check("landscape cells keep 512 width", cw == 512 and ch == 288)
    check("landscape sheet fits the cap", cols * cw <= sheets.MAX_SHEET_SIDE and rows * ch <= sheets.MAX_SHEET_SIDE)
    cw, ch, cols, rows = sheets.sheet_geometry(512, 910)  # portrait 9:16 at 512 wide
    check("portrait grid is 4x2", (cols, rows) == (4, 2))
    check("portrait sheet long side capped at 1568 (was ~2048)",
          max(cols * cw, rows * ch) <= sheets.MAX_SHEET_SIDE)
    check("portrait cells keep their aspect", abs(cw / ch - 512 / 910) < 0.02)
    cw, ch, cols, rows = sheets.sheet_geometry(320, 40)  # extreme strip
    check("cell height floor of 160 survives", ch >= 160)
    cw, ch, cols, rows = sheets.sheet_geometry(256, 144)  # --width 256
    check("small frames are never upscaled", cw == 256)
    done()


# --- OCR: one undecodable frame must not fail the run (item 7) -------------
_GOOD_JPEG_B64 = (
    "/9j/4AAQSkZJRgABAgAAAQABAAD//gAQTGF2YzYyLjI4LjEwMAD/2wBDAAgGBgcGBwgICAgICAkJ"
    "CQoKCgkJCQkKCgoKCgoMDAwKCgoKCgoKDAwMDA0ODQ0NDA0ODg8PDxISEREVFRUZGR//xABNAAEB"
    "AAAAAAAAAAAAAAAAAAAABwEBAQEAAAAAAAAAAAAAAAAAAAQGEAEAAAAAAAAAAAAAAAAAAAAAEQEA"
    "AAAAAAAAAAAAAAAAAAAA/8AAEQgAtAFAAwEiAAIRAAMRAP/aAAwDAQACEQMRAD8AhwDfpAAAAAAA"
    + "A" * 76 * 6 + "AAAAAAAAAAAAAAAH/9k="
)


def _has_vision() -> bool:
    import importlib.util
    return importlib.util.find_spec("Vision") is not None and importlib.util.find_spec("Quartz") is not None


def test_ocr_tolerates_bad_frame():
    section("OCR per-frame tolerance")
    if not _has_vision():
        skip("pyobjc Vision/Quartz not importable in this interpreter")
    import base64
    import ocr
    ad = tmpdir()
    fdir = ad / "frames"
    fdir.mkdir()
    good = base64.b64decode(_GOOD_JPEG_B64)
    (fdir / "frame_0000_t00m00s.jpg").write_bytes(good)
    (fdir / "frame_0001_t00m02s.jpg").write_bytes(good[:64])  # truncated: ImageIO returns None
    (fdir / "frame_0002_t00m04s.jpg").write_bytes(good)
    common.write_json(ad / "frames.json", {"count": 3, "frames": [
        {"index": 0, "t": 0.0, "t_hms": "00:00", "file": "frame_0000_t00m00s.jpg"},
        {"index": 1, "t": 2.0, "t_hms": "00:02", "file": "frame_0001_t00m02s.jpg"},
        {"index": 2, "t": 4.0, "t_hms": "00:04", "file": "frame_0002_t00m04s.jpg"},
    ]})
    try:
        res = ocr.ocr_frames(ad, "en-US")
    except Exception as e:  # noqa: BLE001
        check(f"ocr_frames survived a truncated frame (raised {type(e).__name__})", False)
        done()
        return
    check("all three frames are in the result", res["count"] == 3)
    bad = res["frames"][1]
    check("bad frame has empty lines", bad["lines"] == [] and bad["min_confidence"] is None)
    check("bad frame records the error", isinstance(bad.get("error"), str) and bad["error"])
    check("good frames carry no error", "error" not in res["frames"][0] and "error" not in res["frames"][2])
    check("result counts the failures", res.get("errors") == 1)
    from assemble import build_digest
    meta = {"source": "x", "duration": 5.0, "duration_hms": "00:05", "has_video": True,
            "width": 320, "height": 180, "fps": 30.0}
    fr = common.read_json(ad / "frames.json")
    fr.update({"max_gap": 2.0, "thinned": False, "window": None})
    d = build_digest(ad, meta, fr, res, {"source": "none", "segments": [], "segment_count": 0})
    check("digest reports the failed frame count", "1 frame" in d and "failed" in d)
    done()


# --- item 12: atomic watch.md, wav cleanup on failure, empty swift version --
def test_atomic_text_write():
    section("atomic text write")
    wd = tmpdir()
    common.write_text_atomic(wd / "watch.md", "# digest\n")
    check("write_text_atomic round-trips", (wd / "watch.md").read_text() == "# digest\n")
    check("write_text_atomic leaves no tmp file", not (wd / "watch.md.tmp").exists())
    import assemble
    src = Path(assemble.__file__).read_text()
    check("assemble.py writes watch.md atomically", "write_text_atomic(ad / \"watch.md\"" in src)
    done()


def test_wav_unlinked_when_transcriber_fails():
    section("wav cleanup")
    wd = tmpdir()
    wav = wd / "audio_16k.wav"

    def reply(cmd):
        if cmd[0] == transcribe.FFMPEG:
            wav.write_bytes(b"RIFF")
            return ""
        return RuntimeError("transcribe crashed")
    orig_run, orig_bin = transcribe.run, transcribe.TRANSCRIBE
    transcribe.run, transcribe.TRANSCRIBE = _Stub(reply), sys.executable  # any existing path
    try:
        raises("speech_transcribe propagates the failure",
               lambda: transcribe.speech_transcribe("v.mp4", wd, "en-US"), RuntimeError)
    finally:
        transcribe.run, transcribe.TRANSCRIBE = orig_run, orig_bin
    check("intermediate wav removed even on failure", not wav.exists())
    done()


def test_setup_preflight_empty_swift_output():
    section("setup preflight")
    import subprocess
    import setup

    def sh(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    orig_sh, orig_which = setup.sh, setup.shutil.which
    setup.sh, setup.shutil.which = sh, lambda n: f"/usr/bin/{n}"
    try:
        try:
            setup.preflight()
            check("preflight survives an empty `swift --version`", True)
        except IndexError:
            check("preflight survives an empty `swift --version`", False)
    finally:
        setup.sh, setup.shutil.which = orig_sh, orig_which
    done()


# --- purge removes the URL from url_ids.json; --purge-history (item 13) ----
def test_purge_forgets_url():
    section("purge + history")
    import watch
    url = "https://x/v13"
    mapping = {url: "url_v13", "https://x/other": "url_other"}
    common.CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    common.write_json(common.URL_ID_MAP, mapping)
    wd = common.work_dir("url_v13")
    (wd / "meta.json").write_text("{}")
    (common.CACHE_ROOT / "bin").mkdir(exist_ok=True)
    (common.CACHE_ROOT / "bin" / "ffmpeg").write_bytes(b"#!/bin/sh\n")
    orig = common.run
    common.run = _Stub(lambda cmd: RuntimeError("network must not be used"))
    try:
        watch.purge(url)
    finally:
        common.run = orig
    check("purge removed the work dir", not wd.exists())
    left = common.read_json(common.URL_ID_MAP)
    check("purge removed the URL from url_ids.json", url not in left)
    check("purge kept the other URL", "https://x/other" in left)
    check("purge never touches bin/", (common.CACHE_ROOT / "bin" / "ffmpeg").exists())
    watch.purge_history()
    check("--purge-history clears url_ids.json",
          not common.URL_ID_MAP.exists() or common.read_json(common.URL_ID_MAP) == {})
    check("--purge-history leaves bin/ alone", (common.CACHE_ROOT / "bin" / "ffmpeg").exists())
    done()


# --- preflight names the interpreter (item 15) -----------------------------
def test_preflight_names_interpreter():
    section("preflight interpreter")
    import importlib.util
    import watch
    real = importlib.util.find_spec

    def fake(name, *a, **k):
        return None if name in ("Vision", "Quartz") else real(name, *a, **k)
    watch.importlib.util.find_spec = fake
    try:
        try:
            watch._preflight(is_url=False)
            msg = ""
        except RuntimeError as e:
            msg = str(e)
    finally:
        watch.importlib.util.find_spec = real
    check("missing pyobjc is reported", "pyobjc-framework-Vision" in msg)
    check("the message names sys.executable", sys.executable in msg)
    check("the message gives the exact pip command",
          f'"{sys.executable}" -m pip install' in msg and "pyobjc-framework-Quartz" in msg)
    done()


# --- cache key excludes assembly-only params (item 18) ---------------------
def _done_dir(**over) -> tuple[Path, dict]:
    ad = tmpdir()
    params = {"version": common.VERSION_TAG, "scene": 0.3, "floor": 2.0, "width": 512,
              "max_frames": 300, "locale": "en-US", "repull": True, "threshold": 0.5,
              "start": None, "end": None}
    stored = dict(params)
    stored.update(over)
    common.write_json(ad / "done.json", stored)
    common.write_json(ad / "frames.json", {"count": 0, "frames": []})
    (ad / "watch.md").write_text("# cached\n")
    return ad, params


def test_cache_key_ignores_assembly_params():
    section("cache key")
    import watch
    ad, params = _done_dir()
    check("identical params hit", watch._cache_hit(ad, params))
    ad, params = _done_dir(repull=False)
    check("--no-repull follow-up hits the extraction cache", watch._cache_hit(ad, params))
    ad, params = _done_dir(threshold=0.9)
    check("--threshold change hits the extraction cache", watch._cache_hit(ad, params))
    ad, params = _done_dir(scene=0.1)
    check("--scene change still misses", not watch._cache_hit(ad, params))
    ad, params = _done_dir(locale="fr-FR")
    check("--locale change still misses", not watch._cache_hit(ad, params))
    ad, params = _done_dir(start=3.0)
    check("window change still misses", not watch._cache_hit(ad, params))
    check("repull/threshold are not cache keys",
          "repull" not in watch.CACHE_KEYS and "threshold" not in watch.CACHE_KEYS)
    done()


# --- a failed transcript surfaces without waiting for OCR (item 20) --------
def test_first_exception_wins():
    section("FIRST_EXCEPTION")
    import threading
    import time
    import watch
    stop = threading.Event()

    def slow_ok():
        for _ in range(40):  # 2s unless told to stop
            if stop.is_set():
                return
            time.sleep(0.05)

    def fast_fail():
        time.sleep(0.05)
        raise RuntimeError("transcribe blew up")
    t0 = time.monotonic()
    raises("the first exception propagates",
           lambda: watch._run_concurrently(slow_ok, fast_fail, stop=stop), RuntimeError)
    dt = time.monotonic() - t0
    check(f"it surfaced in {dt:.2f}s, not after the 2s task", dt < 1.0)
    check("the stop flag was raised for the survivor", stop.is_set())
    done()


# --- digest size: frames dir once + basenames; estimated timestamps (item 21)
def _two_frame_inputs():
    meta = {"source": "x.mp4", "duration": 5.0, "duration_hms": "00:05", "has_video": True,
            "width": 640, "height": 360, "fps": 30.0}
    fr = {"count": 2, "max_gap": 2.0, "thinned": False, "window": None, "frames": [
        {"index": 0, "t": 0.0, "t_hms": "00:00", "file": "frame_0000_t00m00s.jpg"},
        {"index": 1, "t": 2.0, "t_hms": "00:02", "file": "frame_0001_t00m02s.jpg"}]}
    ocr = {"engine": "apple-vision", "count": 2, "frames": [
        {"index": 0, "t": 0.0, "t_hms": "00:00", "file": "frame_0000_t00m00s.jpg",
         "lines": [{"text": "HELLO", "confidence": 0.9, "bbox": [0, 0, 1, 1]}],
         "text": "HELLO", "min_confidence": 0.9, "mean_confidence": 0.9},
        {"index": 1, "t": 2.0, "t_hms": "00:02", "file": "frame_0001_t00m02s.jpg",
         "lines": [], "text": "", "min_confidence": None, "mean_confidence": None,
         "hires_file": "hires/hires_0001.jpg"}]}
    tr = {"source": "speechtranscriber", "segment_count": 1,
          "segments": [{"start": 0.0, "end": 1.0, "text": "hi there"}], "text": "hi there"}
    return meta, fr, ocr, tr


def test_digest_frames_dir_once():
    section("digest frame lines")
    from assemble import build_digest
    ad = tmpdir()
    meta, fr, ocr, tr = _two_frame_inputs()
    d = build_digest(ad, meta, fr, ocr, tr)
    fdir = str(ad / "frames")
    check("frames dir printed exactly once", d.count(fdir) == 1)
    check("frame lines use basenames with the t= tag", "t=00:00  frame_0000_t00m00s.jpg" in d)
    check("hi-res re-pull stays relative to the frames dir",
          "t=00:02  hires/hires_0001.jpg  (hi-res re-pull)" in d)
    check("no estimated-timestamps warning by default", "ESTIMATED" not in d)
    fr["timestamps_estimated"] = True
    d = build_digest(ad, meta, fr, ocr, tr)
    check("grid fallback is surfaced in the digest", "ESTIMATED" in d)
    done()


def test_pair_times_grid_fallback():
    section("pair_times")
    files = ["a", "b", "c", "d"]
    times, est = frames.pair_times([0.0, 1.0, 2.0, 3.0], files, offset=0.0, span=None, duration=8.0)
    check("matching counts keep showinfo times", times == [0.0, 1.0, 2.0, 3.0] and est is False)
    times, est = frames.pair_times([0.0, 1.0], files, offset=0.0, span=None, duration=8.0)
    check("a mismatch falls back to an even grid and says so", est is True and len(times) == 4)
    check("grid spans the duration", times[0] == 0.0 and times[-1] == 6.0)
    times, est = frames.pair_times([], files, offset=10.0, span=4.0, duration=100.0)
    check("grid honours the window offset + span", times == [10.0, 11.0, 12.0, 13.0] and est)
    done()


# --- --summary-only (item 22) ----------------------------------------------
def test_summary_only_digest():
    section("summary-only")
    from assemble import build_digest
    ad = tmpdir()
    meta, fr, ocr, tr = _two_frame_inputs()
    sheets = {"cols": 3, "rows": 4, "count": 1,
              "sheets": [{"file": "sheets/sheet_00.jpg", "start_hms": "00:00", "end_hms": "00:02",
                          "frame_indices": [0, 1]}]}
    d = build_digest(ad, meta, fr, ocr, tr, sheets, summary_only=True)
    check("summary keeps the header", "# Video: x.mp4" in d and "duration:" in d)
    check("summary keeps the transcript", "hi there" in d)
    check("summary keeps the OCR text", "HELLO" in d)
    check("summary has no Frames section", "## Frames" not in d)
    check("summary lists no image paths", ".jpg" not in d)
    full = build_digest(ad, meta, fr, ocr, tr, sheets)
    check("default digest still lists frames + sheets", "## Frames" in full and "sheet_00.jpg" in full)
    done()


# --- locale normalisation + validation (item 16) ---------------------------
def test_normalize_locale():
    section("normalize_locale")
    cases = {"en_US": "en-US", "en-us": "en-US", "EN-US": "en-US", "en": "en",
             "fr_fr": "fr-FR", "zh-hans-cn": "zh-Hans-CN", "zh_Hant": "zh-Hant",
             "pt-br": "pt-BR", "es-419": "es-419", " ja-JP ": "ja-JP"}
    for raw, want in cases.items():
        check(f"normalize_locale({raw!r}) == {want!r}", common.normalize_locale(raw) == want)
    for bad in ("", "english", "en-USA-", "e", "en_", "12-US", "en--US"):
        raises(f"normalize_locale rejects {bad!r}", lambda b=bad: common.normalize_locale(b), ValueError)
    # the cache key must see the normalized value (en_US and en-us forked before)
    import argparse
    import watch
    ns = argparse.Namespace(scene=0.3, floor=None, width=512, max_frames=300, locale="en_us",
                            no_repull=False, threshold=0.5, start=None, end=None)
    check("_params normalises the locale for the cache key", watch._params(ns)["locale"] == "en-US")
    check("locale_matches: exact", common.locale_matches("en-US", ["en-US", "fr-FR"]))
    check("locale_matches: case/underscore-insensitive", common.locale_matches("en_us", ["en-US"]))
    check("locale_matches: language-only entry covers a region", common.locale_matches("zh-Hans-CN", ["zh-Hans"]))
    check("locale_matches: unknown", not common.locale_matches("xx-XX", ["en-US"]))
    done()


def test_validate_locale_messages():
    section("validate_locale")
    import watch
    speech = ["en-US", "fr-FR", "ja-JP"]
    vision = ["en-US", "fr-FR", "zh-Hans"]
    check("supported by both -> no error", watch.validate_locale("fr-FR", speech, vision) == [])
    raises("unsupported by speech -> clear error",
           lambda: watch.validate_locale("xx-XX", speech, vision), ValueError)
    try:
        watch.validate_locale("xx-XX", speech, vision)
    except ValueError as e:
        check("error lists the supported speech locales", "fr-FR" in str(e) and "SpeechTranscriber" in str(e))
    warns = watch.validate_locale("ja-JP", speech, vision)
    check("speech-only locale -> OCR warning, not an error", len(warns) == 1 and "Vision" in warns[0])
    check("unknown lists (old binary) -> no verdict", watch.validate_locale("xx-XX", None, None) == [])
    done()


# --- binaries live outside the cache; resolution order (item 5) ------------
def test_bin_resolution_order():
    section("bin resolution")
    home = Path.home()
    check("default bin dir is ~/.local/share/claude-video-mac/bin",
          common.DEFAULT_BIN_DIR == home / ".local" / "share" / "claude-video-mac" / "bin")
    check("legacy shared dir is ~/.cache/claude-video-mac/bin",
          common.LEGACY_SHARED_BIN_DIR == home / ".cache" / "claude-video-mac" / "bin")
    dirs = common.bin_search_dirs()
    check("WATCH_BIN_DIR override is searched first", dirs[0] == common.SHARED_BIN_DIR)
    idx = [dirs.index(common.DEFAULT_BIN_DIR), dirs.index(common.LEGACY_SHARED_BIN_DIR),
           dirs.index(common.BIN_DIR)]
    check("order: override -> new default -> legacy shared -> in-repo bin", idx == sorted(idx))
    d1, d2, d3 = tmpdir(), tmpdir(), tmpdir()
    (d3 / "ffmpeg").write_text("x")
    check("the first dir holding the binary wins",
          common.resolve_binary("ffmpeg", dirs=[d1, d2, d3], which=lambda n: None) == str(d3 / "ffmpeg"))
    (d2 / "ffmpeg").write_text("x")
    check("an earlier dir beats a later one",
          common.resolve_binary("ffmpeg", dirs=[d1, d2, d3], which=lambda n: None) == str(d2 / "ffmpeg"))
    check("PATH is the last resort",
          common.resolve_binary("ffprobe", dirs=[d1, d2, d3], which=lambda n: "/opt/x/ffprobe") == "/opt/x/ffprobe")
    check("missing everywhere -> the expected path in the first dir",
          common.resolve_binary("ffprobe", dirs=[d1, d2, d3], which=lambda n: None) == str(d1 / "ffprobe"))
    done()


def test_cache_size_excludes_bin():
    section("cache size")
    root = tmpdir()
    (root / "bin").mkdir()
    (root / "bin" / "ffmpeg").write_bytes(b"\0" * 1_000_000)
    (root / "local_x").mkdir()
    (root / "local_x" / "f.jpg").write_bytes(b"\0" * 1000)
    (root / "url_ids.json").write_text("{}")
    check("cache_size_bytes excludes bin/ (the legacy shared dir nests in the cache root)",
          common.cache_size_bytes(root) == 1002)
    check("dir_size_bytes measures the bin dir on its own", common.dir_size_bytes(root / "bin") == 1_000_000)
    check("dir_size_bytes of a missing dir is 0", common.dir_size_bytes(root / "nope") == 0)
    done()


# --- setup.py: migration, rebuild skip, arch check, download hygiene (items 5, 19, 26)
def _completed(cmd, rc=0, out="", err=""):
    import subprocess
    return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr=err)


def test_setup_migration_moves_legacy():
    section("setup migration")
    import setup
    new, legacy_shared, legacy_repo = tmpdir() / "bin", tmpdir() / "bin", tmpdir() / "bin"
    legacy_shared.mkdir()
    legacy_repo.mkdir()
    for d in (legacy_shared, legacy_repo):
        (d / "ffmpeg").write_text("#!/bin/sh\necho ffmpeg version 8.1\n")
        (d / "ffmpeg").chmod(0o755)
    orig = setup.is_native_binary
    setup.is_native_binary = lambda p: True
    try:
        got = setup.migrate_legacy("ffmpeg", new, [legacy_shared, legacy_repo])
    finally:
        setup.is_native_binary = orig
    check("binary moved into the new dir", got == new / "ffmpeg" and (new / "ffmpeg").exists())
    check("legacy shared copy is gone (moved, not copied)", not (legacy_shared / "ffmpeg").exists())
    check("in-repo legacy copy removed once the new dir works", not (legacy_repo / "ffmpeg").exists())
    check("nothing to migrate -> None", setup.migrate_legacy("ffprobe", new, [legacy_shared, legacy_repo]) is None)
    (legacy_repo / "ffprobe").write_text("x")
    setup.is_native_binary = lambda p: False
    try:
        got = setup.migrate_legacy("ffprobe", new, [legacy_shared, legacy_repo])
    finally:
        setup.is_native_binary = orig
    check("a non-native legacy binary is neither migrated nor deleted",
          got is None and not (new / "ffprobe").exists() and (legacy_repo / "ffprobe").exists())
    done()


def test_setup_skips_swift_rebuild_when_unchanged():
    section("setup swift rebuild skip")
    import setup
    bin_dir = tmpdir() / "bin"
    bin_dir.mkdir()
    (bin_dir / "transcribe").write_text("#!/bin/sh\n")
    (bin_dir / setup.SRC_HASH_NAME).write_text(setup._sha256(setup.SWIFT_SRC))
    calls: list = []

    def sh(cmd, **kw):
        calls.append(list(cmd))
        return _completed(cmd)
    orig = (setup.sh, setup.is_native_binary, setup.shutil.which)
    # legacy_dirs=[] ALWAYS: migrate_legacy MOVES binaries, and the real legacy
    # dirs are this machine's live install. which() stubbed so CI (no swiftc)
    # exercises the same branch.
    setup.sh, setup.is_native_binary, setup.shutil.which = sh, (lambda p: True), (lambda n: f"/usr/bin/{n}")
    try:
        ok_ = setup.build_transcriber(False, bin_dir=bin_dir, legacy_dirs=[])
        check("unchanged main.swift + present binary -> ok without compiling", ok_ is True)
        check("swiftc NOT invoked", not any(c and c[0] == "swiftc" for c in calls))
        (bin_dir / setup.SRC_HASH_NAME).write_text("stale")
        calls.clear()
        setup.build_transcriber(False, bin_dir=bin_dir, legacy_dirs=[])
        check("changed main.swift -> swiftc invoked", any(c and c[0] == "swiftc" for c in calls))
        check("sidecar hash refreshed after the build",
              (bin_dir / setup.SRC_HASH_NAME).read_text().strip() == setup._sha256(setup.SWIFT_SRC))
    finally:
        setup.sh, setup.is_native_binary, setup.shutil.which = orig
    check("test isolation: setup.BIN_DIR points into the throwaway dir",
          str(setup.BIN_DIR).startswith(str(_ISOLATED)))
    done()


def test_setup_download_timeout_and_zip_cleanup():
    section("setup download hygiene")
    import hashlib
    import io
    import urllib.request
    import setup
    seen: dict = {}

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(url, *a, **kw):
        seen.update(kw)
        return _Resp(b"payload")

    def urlretrieve(url, dest, *a, **kw):
        seen["urlretrieve"] = True
        Path(dest).write_bytes(b"payload")
    orig = (urllib.request.urlopen, urllib.request.urlretrieve)
    urllib.request.urlopen, urllib.request.urlretrieve = urlopen, urlretrieve
    dest = tmpdir() / "x.zip"
    try:
        setup._download("https://example.invalid/x.zip", dest, hash_pinned=True)
    finally:
        urllib.request.urlopen, urllib.request.urlretrieve = orig
    check("download passes a positive timeout", isinstance(seen.get("timeout"), (int, float)) and seen["timeout"] > 0)
    check("urlretrieve (no timeout) is not used", "urlretrieve" not in seen)
    check("payload written", dest.exists() and dest.read_bytes() == b"payload")

    bin_dir = tmpdir() / "bin"
    garbage = b"not a zip archive"
    sha = hashlib.sha256(garbage).hexdigest()
    orig2 = (setup._download, setup.is_native_binary)
    setup._download = lambda url, d, hash_pinned=True: d.write_bytes(garbage)
    setup.is_native_binary = lambda p: True
    try:
        try:
            r = setup._fetch_binary("https://example.invalid/ffmpeg.zip", sha, "ffmpeg",
                                    bin_dir=bin_dir, legacy_dirs=[])
        except Exception as e:  # noqa: BLE001
            r = f"raised {type(e).__name__}"
    finally:
        setup._download, setup.is_native_binary = orig2
    check(f"a bad zip is a reported failure, not a traceback (got {r!r})", r is False)
    check("the zip is removed after the failure", not (bin_dir / "ffmpeg.zip").exists())
    done()


def test_setup_prebuilt_transcriber():
    section("setup prebuilt transcribe")
    import hashlib
    import setup
    bin_dir = tmpdir() / "bin"
    blob = b"\xcf\xfa\xed\xfe fake mach-o"
    good = hashlib.sha256(blob).hexdigest()
    calls: list = []

    def sh(cmd, **kw):
        calls.append(list(cmd))
        return _completed(cmd)
    orig = (setup._download, setup.sh, setup.is_native_binary)
    setup._download = lambda url, d, hash_pinned=True: d.write_bytes(blob)
    setup.sh, setup.is_native_binary = sh, lambda p: True
    url = "https://example.invalid/transcribe"
    try:
        bad = setup.install_prebuilt_transcriber(url, "00" * 32, bin_dir=bin_dir)
        check("SHA mismatch refuses the install", bad is False and not (bin_dir / "transcribe").exists())
        check("no stray download left behind", not list(bin_dir.glob("*")) if bin_dir.exists() else True)
        r = setup.install_prebuilt_transcriber(url, good.upper(), bin_dir=bin_dir)
        check("SHA match (case-insensitive) installs", r is True and (bin_dir / "transcribe").read_bytes() == blob)
        check("installed binary is executable", os.access(bin_dir / "transcribe", os.X_OK))
        check("source-hash sidecar written so a plain setup run does not rebuild over it",
              (bin_dir / setup.SRC_HASH_NAME).read_text().strip() == setup._sha256(setup.SWIFT_SRC))
        check("ad-hoc codesign attempted", any(c and c[0] == "codesign" for c in calls))
    finally:
        setup._download, setup.sh, setup.is_native_binary = orig
    done()


def test_setup_arch_check():
    section("setup arch check")
    import platform
    import setup
    archs = setup.binary_archs(sys.executable)
    check(f"binary_archs reads a real executable ({archs})", bool(archs))
    check("is_native_binary agrees with platform.machine()",
          setup.is_native_binary(sys.executable) == (platform.machine() in archs))
    script = tmpdir() / "fake"
    script.write_text("#!/bin/sh\n")
    check("a shell script has no architecture", setup.binary_archs(script) == [])
    check("...and is therefore not a native binary", setup.is_native_binary(script) is False)
    done()


# --- chapters + metadata from the ONE yt-dlp metadata call (item 23) -------
_PRINT_LINE = (
    'Youtube.abc123\t"My Title"\t"Uploader Name"\t"20240102"\t123.5\t"line1\\nline2"\t'
    '[{"start_time": 0.0, "end_time": 60.0, "title": "Intro"}, '
    '{"start_time": 60.0, "end_time": 123.5, "title": "Demo"}]'
)


def test_url_print_line_parser():
    section("yt-dlp --print parser")
    vid, meta = common.parse_url_print_line(_PRINT_LINE)
    check("id is the first field", vid == "Youtube.abc123")
    check("title decoded", meta.get("title") == "My Title")
    check("uploader decoded", meta.get("uploader") == "Uploader Name")
    check("upload_date normalised to ISO", meta.get("upload_date") == "2024-01-02")
    check("duration is a float", meta.get("duration") == 123.5)
    check("description keeps its newline", meta.get("description") == "line1\nline2")
    check("chapters normalised to start/end/title",
          meta.get("chapters") == [{"start": 0.0, "end": 60.0, "title": "Intro"},
                                   {"start": 60.0, "end": 123.5, "title": "Demo"}])
    vid, meta = common.parse_url_print_line("Vimeo.987")
    check("id-only line (old template / other tools) -> empty meta", vid == "Vimeo.987" and meta == {})
    vid, meta = common.parse_url_print_line('Youtube.x\t"T"\tNOTJSON\tnull\tnull\tnull\tnull')
    check("a broken field drops only itself", vid == "Youtube.x" and meta.get("title") == "T"
          and "uploader" not in meta and "chapters" not in meta)
    vid, meta = common.parse_url_print_line("")
    check("empty line -> no id", vid is None and meta == {})
    check("chapter_starts lists chapter start times",
          common.chapter_starts({"chapters": [{"start": 0.0}, {"start": 60.0}]}) == [0.0, 60.0])
    check("chapter_starts on local meta -> []", common.chapter_starts({}) == [])
    argv = common.ytdlp_id_argv("https://x/v")
    tmpl = argv[argv.index("--print") + 1]
    check("one --print carries id + title + uploader + upload_date + duration + description + chapters",
          all(f in tmpl for f in ("%(extractor_key)s.%(id)s", "%(title)j", "%(uploader)j",
                                  "%(upload_date)j", "%(duration)j", "%(description)j", "%(chapters)j")))
    done()


def test_video_id_for_stores_url_meta():
    section("url meta persisted")
    url = "https://www.youtube.com/watch?v=meta23"
    stub = _Stub(lambda cmd: _PRINT_LINE.replace("abc123", "meta23") + "\n")
    orig = common.run
    common.run = stub
    try:
        vid = common.video_id_for(url)
    finally:
        common.run = orig
    check("id unaffected by the extra fields", vid == "url_Youtube_meta23")
    um = common.work_dir(vid, create=False) / "url_meta.json"
    check("url_meta.json written next to the cache entry", um.exists())
    if um.exists():
        m = common.read_json(um)
        check("…with the title and chapters", m.get("title") == "My Title" and len(m.get("chapters", [])) == 2)
    # download() merges it into meta.json for a URL source with media already present
    import download
    wd = common.work_dir(vid)
    (wd / "source.mp4").write_bytes(b"\0" * 8)
    _vtt(wd, "en")
    common.write_json(wd / "meta.json", {"captions_kind": "manual", "captions_locale": "en-US"})
    orig = download.run
    download.run = _download_stub([])
    try:
        meta = download.download(url, wd, force=False, locale="en-US")
    finally:
        download.run = orig
    check("meta.json carries title/uploader/upload_date/chapters",
          meta.get("title") == "My Title" and meta.get("uploader") == "Uploader Name"
          and meta.get("upload_date") == "2024-01-02" and len(meta.get("chapters", [])) == 2)
    done()


def test_select_forces_chapter_starts():
    section("forced sample points")
    sel = frames.build_select(0.3, 2.0, force_times=[65.0, 130.0, 10.0, 999.0], offset=60.0, span=100.0)
    check("scene + floor terms kept", "gt(scene\\,0.3)" in sel and "gte(t-prev_selected_t\\,2.0)" in sel)
    check("chapter at 65s is window-relative 5.000", "gte(t\\,5.000)*lt(prev_t\\,5.000)" in sel)
    check("chapter at 130s -> 70.000", "gte(t\\,70.000)*lt(prev_t\\,70.000)" in sel)
    check("chapters before the window are dropped", "-50.000" not in sel)
    check("chapters after the window are dropped", "939.000" not in sel)
    plain = frames.build_select(0.3, 2.0)
    check("no forced points -> the classic expression", plain == "select='eq(n\\,0)+gt(scene\\,0.3)+gte(t-prev_selected_t\\,2.0)'")
    done()


def test_digest_video_and_chapters_blocks():
    section("digest video/chapters")
    from assemble import build_digest
    ad = tmpdir()
    meta, fr, ocr, tr = _two_frame_inputs()
    d = build_digest(ad, meta, fr, ocr, tr)
    check("local file: no Video block", "## Video" not in d)
    check("local file: no Chapters block", "## Chapters" not in d)
    meta.update({"source": "https://x/v", "title": "My Title", "uploader": "Uploader Name",
                 "upload_date": "2024-01-02",
                 "chapters": [{"start": 0.0, "end": 60.0, "title": "Intro"},
                              {"start": 60.0, "end": 123.5, "title": "Demo"}]})
    d = build_digest(ad, meta, fr, ocr, tr)
    check("Video block lists title/uploader/date/duration",
          "## Video" in d and "My Title" in d and "Uploader Name" in d and "2024-01-02" in d)
    check("Chapters block lists start -> title", "## Chapters" in d and "01:00  Demo" in d and "00:00  Intro" in d)
    s = build_digest(ad, meta, fr, ocr, tr, summary_only=True)
    check("summary-only keeps Video + Chapters", "## Video" in s and "## Chapters" in s)
    done()


# --- doctor (item 24) --------------------------------------------------------
def _fake_probes(**over):
    probes = {
        "macos": lambda: "26.1",
        "machine": lambda: "arm64",
        "binary": lambda name: {"path": f"/opt/bin/{name}", "archs": ["arm64"], "native": True,
                                "version": "8.1" if name != "transcribe" else "1.6.0+ (--locales)"},
        "videotoolbox": lambda ffmpeg_path: True,
        "pyobjc": lambda: {"Vision": True, "Quartz": True},
        "speech_locales": lambda: ["en-US", "fr-FR"],
        "vision_languages": lambda: ["en-US", "fr-FR"],
        "cache": lambda: {"root": "/c", "media_bytes": 2_000_000, "bin_dir": "/opt/bin",
                          "bin_bytes": 100_000_000, "url_ids": 3, "legacy_bin_present": False},
    }
    probes.update(over)
    return probes


def test_doctor_report():
    section("doctor")
    import doctor
    rep = doctor.collect(_fake_probes())
    check("healthy fake system is healthy", doctor.is_healthy(rep) and not rep["problems"])
    text = doctor.render(rep)
    for needle in (sys.executable, "ffmpeg", "ffprobe", "transcribe", "arm64", "VideoToolbox",
                   "Vision", "Quartz", "speech locales", "Vision languages", "url_ids", "bin dir"):
        check(f"report mentions {needle!r}", needle in text)
    check("cache size is split media/bin", "2 MB" in text and "100 MB" in text)
    rep = doctor.collect(_fake_probes(binary=lambda n: None if n == "ffmpeg" else _fake_probes()["binary"](n)))
    check("missing ffmpeg -> unhealthy", not doctor.is_healthy(rep))
    check("…and the problem names it", any("ffmpeg" in p for p in rep["problems"]))
    rep = doctor.collect(_fake_probes(pyobjc=lambda: {"Vision": False, "Quartz": True}))
    check("missing pyobjc -> unhealthy with a pip hint", not doctor.is_healthy(rep)
          and any("pip install" in p and "pyobjc-framework-Vision" in p for p in rep["problems"]))
    rep = doctor.collect(_fake_probes(speech_locales=lambda: None))
    check("old transcribe (no --locales) is a warning, not a failure",
          doctor.is_healthy(rep) and any("locales" in w for w in rep["warnings"]))
    rep = doctor.collect(_fake_probes(macos=lambda: "15.6"))
    check("macOS < 26 -> unhealthy", not doctor.is_healthy(rep))
    rep = doctor.collect(_fake_probes(binary=lambda n: dict(_fake_probes()["binary"](n), archs=["x86_64"], native=False)))
    check("non-native binary -> unhealthy", not doctor.is_healthy(rep))
    check("main() exit code follows health", doctor.exit_code(_fake_probes()) == 0
          and doctor.exit_code(_fake_probes(macos=lambda: "15.6")) == 1)
    done()


# --- script runner ----------------------------------------------------------
def _run_all() -> int:
    tests = [fn for name, fn in inspect.getmembers(sys.modules[__name__], inspect.isfunction)
             if name.startswith("test_")]
    # definition order, not alphabetical: the output reads like the file
    tests.sort(key=lambda fn: fn.__code__.co_firstlineno)
    skipped = 0
    for fn in tests:
        try:
            fn()
        except _Skip:
            skipped += 1
        except AssertionError:
            pass  # already recorded by check()
        except Exception as e:  # noqa: BLE001 — a crash is a failure, not a stop
            FAILURES.append(f"{fn.__name__} crashed: {type(e).__name__}: {e}")
            print(f"  ✗ {fn.__name__} crashed: {type(e).__name__}: {e}")
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED")
        return 1
    print(f"all unit tests passed ({len(tests)} groups, {skipped} skipped)")
    return 0


if __name__ == "__main__":
    sys.exit(_run_all())
