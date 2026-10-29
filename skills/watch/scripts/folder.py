"""Folder mode: a folder of takes in one run.

A folder holding two or more media files is watched take by take, each one
cached exactly like a single video, then summed up in one digest: side-by-side
sheets with one row per take (the same moments of every take, one above the
other, so an A/B review is a single read), followed by each take's own digest.

The side-by-side sheets live in the folder's own cache entry
(local_set_<hash>/, or its windows/<span>/ for a --start/--end run), under
compare/<key>/, where the key covers exactly what the sheets show. A re-run
with the same takes reuses them, and a run that sees different takes writes
its own, so a concurrent run can never swap the images under a digest.

Emits compare/<key>/compare.json: {cols, sheets: [{file, first, last}]},
files relative to the set dir, take numbers 1-based.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import sheets as sheets_mod
from common import artifact_dir, fmt_ts, read_json, write_json

COMPARE_COLS = 6       # moments per take, from its first frame to its last
MAX_COMPARE_ROWS = 8   # takes per side-by-side sheet
ROW_GAP = 8            # px of background between takes, so two rows never read as one


def compare_picks(frames: list[dict], cols: int = COMPARE_COLS) -> list[dict]:
    """The frames in one take's row: for each of `cols` moments evenly spaced
    from the take's first kept frame to its last, the nearest kept frame
    (the earlier one on a tie). The same frame can fill two cells: where a
    take holds still, dedup kept one frame for the whole stretch."""
    if not frames:
        return []
    lo, hi = frames[0]["t"], frames[-1]["t"]
    picks = []
    for j in range(cols):
        target = lo + (hi - lo) * j / (cols - 1)
        picks.append(min(frames, key=lambda f: abs(f["t"] - target)))
    return picks


def compare_geometry(iw: int, ih: int, cols: int = COMPARE_COLS) -> tuple[int, int, int]:
    """(cell_w, cell_h, takes per sheet) for frames of iw x ih px: `cols`
    cells across the vision cap, and as many take rows down it as fit. The
    cell is ROW_GAP taller than the frame; aspect-fit centres the frame, so
    the spare height is the gap between rows."""
    cell_w = min(sheets_mod.CELL_WIDTH, iw, sheets_mod.MAX_SHEET_SIDE // cols)
    cell_h = max(1, round(cell_w * ih / iw)) + ROW_GAP
    rows = max(1, min(MAX_COMPARE_ROWS, sheets_mod.MAX_SHEET_SIDE // cell_h))
    return cell_w, cell_h, rows


def take_facts(wd: Path, start: float | None, end: float | None) -> dict:
    """What the folder digest says about a take that ran: its length and
    frames, read from the files its run left in the cache."""
    ad = artifact_dir(wd, start, end, create=False)
    meta = read_json(wd / "meta.json")
    frames = read_json(ad / "frames.json")
    return {
        "duration_hms": meta.get("duration_hms"),
        "has_video": meta.get("has_video", True),
        "frames_dir": ad / "frames",
        "frames": frames.get("frames", []),
    }


def _row_note(take: dict) -> str:
    if "error" in take:
        return "failed"
    return "audio only" if not take.get("has_video", True) else "no frames"


def compare_cells(takes: list[dict], first_no: int = 1) -> list[tuple]:
    """The cells of the side-by-side rows for these takes, row-major: one row
    of COMPARE_COLS per take, in order. A take without frames (failed, audio
    only) keeps its row, blank but labelled, so row N is always take N."""
    cells: list[tuple] = []
    for n, take in enumerate(takes, start=first_no):
        picks = compare_picks(take.get("frames") or [])
        if not picks:
            cells.append((None, f"take {n} · {_row_note(take)}"))
            cells.extend([(None, "")] * (COMPARE_COLS - 1))
            continue
        for j, f in enumerate(picks):
            label = f"take {n} · {f['t_hms']}" if j == 0 else f["t_hms"]
            cells.append((take["frames_dir"] / f["file"], label))
    return cells


def _stamp(p: Path) -> tuple:
    """A picked frame's size and mtime: a re-extraction rewrites the frame
    under the same name, and this is what tells the new pixels apart."""
    try:
        st = p.stat()
        return st.st_size, st.st_mtime_ns
    except OSError:
        return None, None


def compare_key(takes: list[dict], extra: dict | None = None) -> str:
    """Names what the sheets show: every take's frames dir, the frames picked
    from it (name, size, mtime) and its row note, plus `extra` (the
    extraction version and settings)."""
    rows = []
    for t in takes:
        picked = [[c[0].name, *_stamp(c[0])] for c in compare_cells([t]) if c[0]]
        rows.append([str(t.get("frames_dir") or ""), picked,
                     "" if t.get("frames") else _row_note(t)])
    blob = json.dumps({"rows": rows, "extra": extra or {}}, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode()).hexdigest()[:16]


def build_compare(out_dir: Path, takes: list[dict], extra: dict | None = None) -> list[dict]:
    """Render the side-by-side sheets into out_dir/compare/<key>/, or reuse
    them when a run already did, and return their manifest. Call it holding
    the folder's lock, so two runs never write one key at once.
    ponytail: the sheets of takes the folder no longer holds stay until the
    folder's entry ages out (a few hundred KB a set); prune sibling keys if a
    folder is ever re-run with changing takes often."""
    key = compare_key(takes, extra)
    kdir = out_dir / "compare" / key
    manifest = kdir / "compare.json"
    if manifest.exists():
        try:
            prev = read_json(manifest)["sheets"]
            if all((out_dir / s["file"]).exists() for s in prev):
                return prev
        except Exception:  # noqa: BLE001 — unreadable -> rebuild
            pass
    shutil.rmtree(kdir, ignore_errors=True)  # a killed run's partial set
    kdir.mkdir(parents=True)
    first = next((t for t in takes if t.get("frames")), None)
    if first is None:
        write_json(manifest, {"cols": COMPARE_COLS, "sheets": []})
        return []
    import Quartz

    # Geometry from the first take with frames; aspect-fit absorbs the rest.
    img = sheets_mod._cg_image(str(first["frames_dir"] / first["frames"][0]["file"]))
    cell_w, cell_h, per_sheet = compare_geometry(Quartz.CGImageGetWidth(img),
                                                 Quartz.CGImageGetHeight(img))
    out = []
    for si in range(0, len(takes), per_sheet):
        chunk = takes[si : si + per_sheet]
        name = f"compare_{si // per_sheet:02d}.jpg"
        sheets_mod._render_sheet(compare_cells(chunk, si + 1), kdir / name,
                                 cell_w, cell_h, COMPARE_COLS, len(chunk))
        out.append({"file": f"compare/{key}/{name}", "first": si + 1, "last": si + len(chunk)})
    write_json(manifest, {"cols": COMPARE_COLS, "sheets": out})  # last: marks the set complete
    return out


def folder_digest(folder: Path, takes: list[dict], compare: list[dict] | None,
                  compare_dir: Path | None, params: dict, summary_only: bool = False,
                  compare_error: str | None = None) -> str:
    n = len(takes)
    lines: list[str] = []
    a = lines.append
    a(f"# Folder: {folder}")
    a("")
    a(f"- takes: {n}, in name order (take2 before take10)")
    for i, t in enumerate(takes, 1):
        if "error" in t:
            a(f"  {i}. {t['name']} — **failed:** {t['error']}")
        elif not t.get("has_video", True):
            a(f"  {i}. {t['name']} — {t.get('duration_hms')}, audio only")
        else:
            a(f"  {i}. {t['name']} — {t.get('duration_hms')}, {len(t.get('frames') or [])} frames")
    start, end = params.get("start"), params.get("end")
    if start is not None or end is not None:
        label = f"{fmt_ts(start or 0)}–{fmt_ts(end) if end is not None else 'end'}"
        a(f"- **focused window: {label}** (the same window in every take)")
    a("")

    if not summary_only:
        a("## Side by side")
        a("")
        if compare:
            a(f"_One row per take, in the order above. Each row shows {COMPARE_COLS} "
              "moments evenly spaced from the take's first frame to its last; the "
              "first cell names the take and every cell its timestamp. A timestamp "
              "repeated along a row is the same frame: the take held still there. "
              "**Read these first to compare the takes**, then each take's own "
              "sheets and frames below for detail._")
            a("")
            for s in compare:
                rows = f"take {s['first']}" if s["first"] == s["last"] else f"takes {s['first']}–{s['last']}"
                a(f"{rows}  {compare_dir / s['file']}")
        elif compare_error:
            a(f"_(side-by-side sheets skipped: {compare_error}; each take's own "
              "sheets and frames are below)_")
        else:
            a("_(no take has frames, so there is nothing to set side by side)_")
        a("")

    for i, t in enumerate(takes, 1):
        if "error" in t:
            a(take_heading(i, n, t["name"]))
            a("")
            a(f"_(this take failed: {t['error']})_")
            a("")
        else:  # rendered with this heading and its sections a level down
            a(t["digest"].rstrip("\n"))
            a("")
    return "\n".join(lines)


def take_heading(i: int, n: int, name: str) -> str:
    return f"## Take {i} of {n}: {name}"
