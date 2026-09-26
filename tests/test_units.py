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
