"""Phase 2b: tile the kept frames into labeled contact sheets.

Reading a long video's frames one Read at a time dominates the token cost of
/watch. A contact sheet packs up to SHEET_COLS x SHEET_ROWS consecutive frames
into one image — with each cell labeled with its source timestamp — so Claude
gets the video's visual structure from one or two Reads and only opens
individual full-size frames for moments that need close inspection.

Rendering uses AppKit/Quartz (pyobjc, already required for OCR and the
perceptual dedup) rather than ffmpeg: the bundled static ffmpeg is not
guaranteed to have drawtext/fontconfig compiled in, and the labels are the
whole point.

Sheets are derived artifacts: sheets/sheet_NN.jpg + sheets.json live next to
frames/ in the run's artifact dir and are rebuilt whenever frames are.

Emits sheets.json: {cols, rows, count, sheets: [{file, start_hms, end_hms,
frame_indices}]}.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from common import (
    artifact_dir,
    log,
    parse_ts,
    read_json,
    video_id_for,
    work_dir,
    write_json,
)

# Below this many frames a sheet saves less than it costs in indirection —
# SKILL.md already tells Claude to read tiny frame sets in full.
MIN_FRAMES = 4
# Cell width matches the default extraction width so sheets never upscale.
CELL_WIDTH = 512
# Grid shape adapts to orientation so a full sheet stays near Claude's
# ~1.5k-px vision cap in both dimensions (landscape 1536x~1160, portrait
# 1152x~1024): larger would be downscaled anyway, smaller wastes the read.
GRID_LANDSCAPE = (3, 4)  # cols, rows -> 12 frames/sheet
GRID_PORTRAIT = (4, 2)   # tall cells -> 8 frames/sheet
JPEG_QUALITY = 0.85


def _cg_image(path: str):
    """Load a CGImage (pixel-exact, no NSImage point/pixel ambiguity)."""
    import Quartz
    from Foundation import NSURL

    src = Quartz.CGImageSourceCreateWithURL(NSURL.fileURLWithPath_(path), None)
    if src is None:
        raise RuntimeError(f"cannot read image: {path}")
    cg = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
    if cg is None:
        raise RuntimeError(f"cannot decode image: {path}")
    return cg


def _render_sheet(cells: list, out_path: Path, cell_w: int, cell_h: int,
                  cols: int, rows: int) -> None:
    """cells: [(frame_path, label)] in time order, row-major from the top."""
    import AppKit
    import Quartz
    from Foundation import NSString

    W, H = cols * cell_w, rows * cell_h
    rep = AppKit.NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, W, H, 8, 4, True, False, AppKit.NSDeviceRGBColorSpace, 0, 0
    )
    ctx = AppKit.NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    AppKit.NSGraphicsContext.saveGraphicsState()
    AppKit.NSGraphicsContext.setCurrentContext_(ctx)
    try:
        cg = ctx.CGContext()
        Quartz.CGContextSetRGBFillColor(cg, 0.09, 0.09, 0.11, 1.0)
        Quartz.CGContextFillRect(cg, Quartz.CGRectMake(0, 0, W, H))

        attrs = {
            AppKit.NSFontAttributeName: AppKit.NSFont.boldSystemFontOfSize_(20),
            AppKit.NSForegroundColorAttributeName: AppKit.NSColor.whiteColor(),
        }

        for i, (fpath, label) in enumerate(cells):
            col, row = i % cols, i // cols
            x = col * cell_w
            y = H - (row + 1) * cell_h  # row 0 at the TOP (flip to CG coords)

            img = _cg_image(str(fpath))
            iw, ih = Quartz.CGImageGetWidth(img), Quartz.CGImageGetHeight(img)
            scale = min(cell_w / iw, cell_h / ih)
            dw, dh = iw * scale, ih * scale
            Quartz.CGContextDrawImage(
                cg,
                Quartz.CGRectMake(x + (cell_w - dw) / 2, y + (cell_h - dh) / 2, dw, dh),
                img,
            )

            # Timestamp chip, top-left of the cell: black box + white bold text.
            text = NSString.stringWithString_(label)
            size = text.sizeWithAttributes_(attrs)
            pad = 5
            bx, by = x + 6, y + cell_h - size.height - 2 * pad - 6
            AppKit.NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.72).setFill()
            AppKit.NSBezierPath.fillRect_(
                ((bx, by), (size.width + 2 * pad, size.height + 2 * pad))
            )
            text.drawAtPoint_withAttributes_((bx + pad, by + pad), attrs)
    finally:
        AppKit.NSGraphicsContext.restoreGraphicsState()

    data = rep.representationUsingType_properties_(
        AppKit.NSBitmapImageFileTypeJPEG,
        {AppKit.NSImageCompressionFactor: JPEG_QUALITY},
    )
    if data is None or not data.writeToFile_atomically_(str(out_path), True):
        raise RuntimeError(f"failed to write {out_path}")


def build(ad: Path, frames: dict | None = None) -> dict:
    """Tile ad/frames/* per the frames.json manifest into ad/sheets/*.

    Returns the sheets manifest (also written to sheets.json). Raises on
    render failure — the caller decides whether sheets are optional.
    """
    import Quartz

    if frames is None:
        frames = read_json(ad / "frames.json")
    manifest = frames.get("frames", [])
    frames_dir = ad / "frames"

    sheets_dir = ad / "sheets"
    sheets_dir.mkdir(exist_ok=True)
    for old in sheets_dir.glob("sheet_*.jpg"):
        old.unlink()

    if len(manifest) < MIN_FRAMES:
        out = {"cols": None, "rows": None, "count": 0, "sheets": [],
               "note": f"fewer than {MIN_FRAMES} frames; read them directly"}
        write_json(ad / "sheets.json", out)
        return out

    # Cell geometry from the first frame; all frames in a run share the
    # extraction width, and aspect-fit absorbs any odd one out.
    first = _cg_image(str(frames_dir / manifest[0]["file"]))
    iw, ih = Quartz.CGImageGetWidth(first), Quartz.CGImageGetHeight(first)
    cell_w = CELL_WIDTH
    cell_h = max(160, round(cell_w * ih / iw))
    cols, rows = GRID_LANDSCAPE if iw >= ih else GRID_PORTRAIT
    per_sheet = cols * rows

    sheets = []
    for si in range(0, len(manifest), per_sheet):
        chunk = manifest[si : si + per_sheet]
        n = si // per_sheet
        name = f"sheet_{n:02d}.jpg"
        cells = [(frames_dir / m["file"], m["t_hms"]) for m in chunk]
        # Last sheet may be partial; shrink its row count so it isn't mostly
        # background (cols stays fixed to keep every sheet the same width).
        chunk_rows = (len(chunk) + cols - 1) // cols
        _render_sheet(cells, sheets_dir / name, cell_w, cell_h, cols, chunk_rows)
        sheets.append({
            "file": f"sheets/{name}",
            "start_hms": chunk[0]["t_hms"],
            "end_hms": chunk[-1]["t_hms"],
            "frame_indices": [m["index"] for m in chunk],
        })

    out = {"cols": cols, "rows": rows, "count": len(sheets), "sheets": sheets}
    write_json(ad / "sheets.json", out)
    log(f"tiled {len(manifest)} frames into {len(sheets)} contact sheet(s) "
        f"({cols}x{rows}, {cell_w}x{cell_h} cells)")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Rebuild contact sheets for an extracted video.")
    ap.add_argument("source", help="video URL or local path (frames must be extracted)")
    ap.add_argument("--start", default=None, help="window start matching the extraction run")
    ap.add_argument("--end", default=None, help="window end matching the extraction run")
    args = ap.parse_args()
    wd = work_dir(video_id_for(args.source))
    start = parse_ts(args.start) if args.start is not None else None
    end = parse_ts(args.end) if args.end is not None else None
    ad = artifact_dir(wd, start, end)
    out = build(ad)
    for s in out["sheets"]:
        print(f"  {s['start_hms']}–{s['end_hms']}  {ad / s['file']}")
    print(f"[{out['count']} sheet(s)]")


if __name__ == "__main__":
    main()
