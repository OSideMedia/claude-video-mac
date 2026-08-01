#!/usr/bin/env python3
"""Unit tests for the pure helpers — no media, no setup.py, no macOS frameworks.

Run:  python3 tests/test_units.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "watch" / "scripts"))

import common  # noqa: E402
import frames  # noqa: E402
import transcribe  # noqa: E402
from assemble import merge_segments  # noqa: E402

FAILURES = []


def check(desc: str, cond: bool):
    print(("  ✓ " if cond else "  ✗ ") + desc)
    if not cond:
        FAILURES.append(desc)


# --- timestamps -------------------------------------------------------------
print("== timestamps ==")
check("parse_ts '90' -> 90", common.parse_ts("90") == 90.0)
check("parse_ts '1:30' -> 90", common.parse_ts("1:30") == 90.0)
check("parse_ts '1:02:03' -> 3723", common.parse_ts("1:02:03") == 3723.0)
check("parse_ts '12.5' -> 12.5", common.parse_ts("12.5") == 12.5)
try:
    common.parse_ts("1:2:3:4")
    check("parse_ts rejects 4-part", False)
except ValueError:
    check("parse_ts rejects 4-part", True)
check("fmt_ts 75 -> 01:15", common.fmt_ts(75) == "01:15")
check("fmt_ts 3723 -> 1:02:03", common.fmt_ts(3723) == "1:02:03")
check("fmt_ts clamps negatives", common.fmt_ts(-3) == "00:00")
check("fmt_vtt_ts 61.5 -> 00:01:01.500", common.fmt_vtt_ts(61.5) == "00:01:01.500")

# --- frames: pts regex + thinning ------------------------------------------
print("== frames ==")
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

# --- vtt parsing ------------------------------------------------------------
print("== vtt ==")
with tempfile.NamedTemporaryFile("w", suffix=".vtt", delete=False, encoding="utf-8") as f:
    f.write("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n<c>it&#39;s</c> a &quot;test&quot;\n\n"
            "00:00:02.000 --> 00:00:03.000\nit's a \"test\"\n\n"
            "00:00:03.000 --> 00:00:04.000\nnext line\n")
    vtt_path = Path(f.name)
segs = transcribe.parse_vtt(vtt_path)
vtt_path.unlink()
check("parse_vtt strips tags + unescapes entities", segs[0]["text"] == 'it\'s a "test"')
check("parse_vtt collapses rolled-up duplicates", len(segs) == 2 and segs[0]["end"] == 3.0)

# --- transcript paragraph merging -------------------------------------------
print("== merge_segments ==")
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

# --- cache identity + artifact dirs -----------------------------------------
print("== cache ==")
wd = Path(tempfile.mkdtemp())
check("artifact_dir full run = wd", common.artifact_dir(wd, None, None) == wd)
w = common.artifact_dir(wd, 3.0, 6.0)
check("artifact_dir window is namespaced", w == wd / "windows" / "3.00-6.00")
check("artifact_dir start-only window", common.artifact_dir(wd, 3.0, None).name == "3.00-end")

# atomic write_json must not leave partial files behind
common.write_json(wd / "x.json", {"a": 1})
check("write_json round-trips", common.read_json(wd / "x.json") == {"a": 1})
check("write_json leaves no tmp file", not (wd / "x.json.tmp").exists())

# --- version consistency ----------------------------------------------------
print("== version consistency ==")
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

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED")
    sys.exit(1)
print("all unit tests passed")
