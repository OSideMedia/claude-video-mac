"""Phase 3: on-device OCR with Apple Vision over the extracted frames.

VNRecognizeTextRequest (accurate) runs entirely on-device. For each frame we
record every recognized line with its confidence and normalized bbox, producing
a timestamped on-screen-text layer (ocr.json) keyed to the frame timestamps.

The per-line confidence is what Phase 5 uses to decide when to re-pull a
high-resolution frame.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import Quartz
import Vision
from Foundation import NSURL

from common import log, read_json, video_id_for, work_dir, write_json


def _load_cgimage(path: str):
    url = NSURL.fileURLWithPath_(path)
    src = Quartz.CGImageSourceCreateWithURL(url, None)
    if src is None:
        raise RuntimeError(f"cannot read image: {path}")
    return Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)


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


def ocr_frames(wd: Path) -> dict:
    frames = read_json(wd / "frames.json")["frames"]
    frames_dir = wd / "frames"
    log(f"OCR over {len(frames)} frames (Apple Vision, on-device)…")

    out_frames = []
    for fr in frames:
        lines = ocr_image(str(frames_dir / fr["file"]))
        confs = [l["confidence"] for l in lines]
        out_frames.append({
            "index": fr["index"],
            "t": fr["t"],
            "t_hms": fr["t_hms"],
            "file": fr["file"],
            "lines": lines,
            "text": " ".join(l["text"] for l in lines),
            "min_confidence": round(min(confs), 3) if confs else None,
            "mean_confidence": round(sum(confs) / len(confs), 3) if confs else None,
        })

    result = {"engine": "apple-vision", "count": len(out_frames), "frames": out_frames}
    write_json(wd / "ocr.json", result)
    n_text = sum(1 for f in out_frames if f["lines"])
    log(f"OCR done: text found on {n_text}/{len(out_frames)} frames")
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", help="video URL/path (frames must exist), or a single image")
    args = ap.parse_args()

    p = Path(args.source)
    if p.exists() and p.suffix.lower() in {".jpg", ".jpeg", ".png"}:
        for l in ocr_image(str(p)):
            print(f"  [{l['confidence']:.2f}] {l['text']}")
        return

    wd = work_dir(video_id_for(args.source))
    res = ocr_frames(wd)
    for f in res["frames"]:
        tag = f"t={f['t_hms']}"
        if f["lines"]:
            print(f"  {tag}: " + " | ".join(f"{l['text']} ({l['confidence']:.2f})" for l in f["lines"]))
        else:
            print(f"  {tag}: (no text)")


if __name__ == "__main__":
    main()
