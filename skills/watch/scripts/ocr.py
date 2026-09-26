"""Phase 3: on-device OCR with Apple Vision over the extracted frames.

VNRecognizeTextRequest (accurate) runs entirely on-device. For each frame we
record every recognized line with its confidence and normalized bbox, producing
a timestamped on-screen-text layer (ocr.json) keyed to the frame timestamps.

The per-line confidence is what Phase 5 uses to decide when to re-pull a
high-resolution frame.
"""
from __future__ import annotations

import argparse
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import objc
import Quartz
import Vision
from Foundation import NSURL

from common import log, read_json, video_id_for, work_dir, write_json


def _load_cgimage(path: str):
    url = NSURL.fileURLWithPath_(path)
    src = Quartz.CGImageSourceCreateWithURL(url, None)
    if src is None:
        raise RuntimeError(f"cannot read image: {path}")
    cg = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
    if cg is None:  # truncated/corrupt JPEG: ImageIO returns None, not an error
        raise RuntimeError(f"cannot decode image: {path}")
    return cg


def ocr_image(path: str, languages=("en-US",)) -> list[dict]:
    """Return recognized lines [{text, confidence, bbox:[x,y,w,h]}] for one image.

    bbox is Vision-normalized (0..1), origin bottom-left.
    """
    cg = _load_cgimage(path)
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg, None)
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    req.setUsesLanguageCorrection_(True)
    if languages:
        req.setRecognitionLanguages_(list(languages))

    ok, err = handler.performRequests_error_([req], None)
    if not ok:
        raise RuntimeError(f"Vision request failed: {err}")

    lines = []
    for obs in req.results() or []:
        cand = obs.topCandidates_(1)
        if not cand:
            continue
        top = cand[0]
        box = obs.boundingBox()  # CGRect, normalized, bottom-left origin
        lines.append({
            "text": str(top.string()),
            "confidence": round(float(top.confidence()), 3),
            "bbox": [
                round(float(box.origin.x), 4),
                round(float(box.origin.y), 4),
                round(float(box.size.width), 4),
                round(float(box.size.height), 4),
            ],
        })
    return lines


def _warm_frameworks() -> None:
    """pyobjc resolves module attributes lazily and NOT thread-safely: the
    first concurrent lookups of e.g. Quartz.CGImageSourceCreateWithURL from
    the worker pool intermittently raise KeyError. Touch every symbol the
    workers use once, on the calling thread, before the pool starts."""
    _ = (Quartz.CGImageSourceCreateWithURL, Quartz.CGImageSourceCreateImageAtIndex,
         Vision.VNImageRequestHandler, Vision.VNRecognizeTextRequest,
         Vision.VNRequestTextRecognitionLevelAccurate, NSURL.fileURLWithPath_)


def ocr_frames(ad: Path, locale: str = "en-US", stop=None) -> dict:
    """OCR every frame listed in `ad`/frames.json. Recognition follows the
    run's locale (with en-US kept as a fallback for mixed-language screens).
    Frames are independent, so requests run in parallel — Vision releases the
    GIL across the ObjC call and this phase is the pipeline's wall-clock tail.
    `stop` (threading.Event) aborts remaining frames when a sibling phase failed."""
    frames = read_json(ad / "frames.json")["frames"]
    frames_dir = ad / "frames"
    languages = [locale] if locale == "en-US" else [locale, "en-US"]
    log(f"OCR over {len(frames)} frames (Apple Vision, on-device)…")
    _warm_frameworks()

    def _one(fr) -> tuple[list[dict], str | None]:
        if stop is not None and stop.is_set():
            return [], "skipped: run aborted"
        # Per-frame tolerance: one truncated JPEG (or a Vision hiccup) must not
        # fail the whole run — record the error on that frame and move on.
        try:
            with objc.autorelease_pool():
                return ocr_image(str(frames_dir / fr["file"]), languages=tuple(languages)), None
        except Exception as e:  # noqa: BLE001
            return [], f"{type(e).__name__}: {e}"[:300]

    workers = min(8, os.cpu_count() or 4)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(_one, frames))

    out_frames = []
    errors = 0
    for fr, (lines, err) in zip(frames, results):
        confs = [l["confidence"] for l in lines]
        rec = {
            "index": fr["index"],
            "t": fr["t"],
            "t_hms": fr["t_hms"],
            "file": fr["file"],
            "lines": lines,
            "text": " ".join(l["text"] for l in lines),
            "min_confidence": round(min(confs), 3) if confs else None,
            "mean_confidence": round(sum(confs) / len(confs), 3) if confs else None,
        }
        if err:
            rec["error"] = err
            errors += 1
            log(f"OCR failed at t={fr['t_hms']} ({fr['file']}): {err}")
        out_frames.append(rec)

    result = {"engine": "apple-vision", "count": len(out_frames), "errors": errors,
              "frames": out_frames}
    write_json(ad / "ocr.json", result)
    n_text = sum(1 for f in out_frames if f["lines"])
    log(f"OCR done: text found on {n_text}/{len(out_frames)} frames"
        + (f", {errors} frame(s) failed" if errors else ""))
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", help="video URL/path (frames must exist), or a single image")
    ap.add_argument("--locale", default="en-US")
    args = ap.parse_args()

    p = Path(args.source)
    if p.exists() and p.suffix.lower() in {".jpg", ".jpeg", ".png"}:
        for l in ocr_image(str(p)):
            print(f"  [{l['confidence']:.2f}] {l['text']}")
        return

    wd = work_dir(video_id_for(args.source))
    res = ocr_frames(wd, args.locale)
    for f in res["frames"]:
        tag = f"t={f['t_hms']}"
        if f["lines"]:
            print(f"  {tag}: " + " | ".join(f"{l['text']} ({l['confidence']:.2f})" for l in f["lines"]))
        else:
            print(f"  {tag}: (no text)")


if __name__ == "__main__":
    main()
