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
