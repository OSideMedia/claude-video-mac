"""Sound labels: the kinds of sound besides speech, and when they happen.

Apple's built-in sound classifier (SoundAnalysis, on-device, 303 kinds of
sound) labels every second of the audio, half a second apart, through the
transcriber's `--sounds` mode. sounds.json keeps each window's labels at
0.3 or more, so the digest's cut-off (CUTOFF) can move without a re-run;
`rows()` turns them into the digest's lines: one kind of sound per span,
spans that start and end together on one line, speech left out (the
transcript has it).
"""
from __future__ import annotations

import json
from pathlib import Path

import transcribe as transcribe_mod
from common import TRANSCRIBE, log, read_json, run, write_json

SOUNDS_JSON = "sounds.json"  # per video, beside transcript.json

CUTOFF = 0.6     # a label counts at this confidence or more (silence reads as faint music)
SKIP = frozenset({"speech", "silence"})  # the transcript covers speech
# Under speech the classifier mostly hears the voice. Measured on the `say`
# voice, 12 generated dialogue takes and effects mixed under the voice: some
# voices read as a buzz (0.6-0.99) or a pig's grunt (0.89-0.95), and a sigh
# where speech dips to 0.53; a door under a voice reads as a typewriter, and
# music mixed 12 dB under it only 0.47-0.54, 18 dB under 0.41-0.45.
# ponytail: measured bars, not a model of masking; move them, or widen
# VOICE_LIKE, when another voice or mix shows a new confusion.
SPEECH = 0.5         # a window has speech in it from here
VOICE_LIKE = frozenset({"mosquito_buzz", "insect", "fly_buzz", "bee_buzz", "pig_oink"})  # never under speech
MASKED_MUSIC = 0.45  # music under speech counts from here
UNDER_SPEECH = 0.8   # any other sound under speech counts from here
BRIDGE = 1.0     # one label's windows this many seconds apart or less are one span
SAME = 0.5       # spans whose starts and ends are this close share a line
MAX_LABELS = 4   # kinds of sound on one line, strongest first
# ponytail: a fixed cap, longest spans kept; rank by kind if a long video's
# short sounds (a gunshot under a music bed) ever matter more than its beds.
MAX_LINES = 20


def classify(video_path: str, wd: Path, stop=None) -> dict:
    """Label the video's audio. An older transcriber has no --sounds mode:
    it takes the flag for a file name and exits 2 with "no such file:
    --sounds". The current one exits 2 for a missing file too, naming the
    wav; that is a failure, never a sign of an older transcriber."""
    wav = wd / "sounds_16k.wav"
    # ponytail: its own wav beside the transcriber's; share one if the second
    # decode ever shows on long videos.
    try:
        transcribe_mod.extract_wav(video_path, wav, stop=stop)
        proc = run([TRANSCRIBE, "--sounds", str(wav)], stop=stop, check=False)
    finally:
        wav.unlink(missing_ok=True)  # a pure intermediate, as the transcriber's is
    if proc.returncode == 2 and "no such file: --sounds" in (proc.stderr or ""):
        return {"source": "unavailable"}
    if proc.returncode != 0:
        raise RuntimeError(f"transcribe --sounds exited {proc.returncode}: "
                           f"{(proc.stderr or '').strip()[-300:]}")
    data = json.loads(proc.stdout)
    windows = [{"start": round(float(w["start"]), 2), "end": round(float(w["end"]), 2),
                "labels": {str(k): round(float(v), 2) for k, v in w["labels"].items()}}
               for w in data["windows"]]
    return {"source": "soundanalysis", "classifier": data.get("classifier"), "windows": windows}


def analyze(wd: Path, stop=None) -> dict | None:
    """Write sounds.json. Never fails the run: a failure is recorded and the
    digest says so. A run that is stopping (a sibling phase failed) writes
    nothing, so no half-made record is reused."""
    meta = read_json(wd / "meta.json")
    if not meta.get("has_audio"):
        rec = {"source": "none"}
    else:
        log("labelling sounds on-device…")
        try:
            rec = classify(meta["video_path"], wd, stop=stop)
        except Exception as e:  # noqa: BLE001 — sound labels help, never block
            if stop is not None and stop.is_set():
                return None
            log(f"warn: sound labels skipped: {e}")
            rec = {"source": "error", "error": str(e)[-500:]}
        rec["transcriber"] = transcribe_mod.transcriber_id()
    write_json(wd / SOUNDS_JSON, rec)
    return rec


def needs_redo(rec: dict | None) -> bool:
    """No record; or one without labels (an older transcriber, a failed
    analysis) made by a transcriber other than the installed one. Like an
    untimed transcript, it is redone once per binary."""
    if not rec:
        return True
    if rec.get("source") not in ("unavailable", "error"):
        return False
    installed = transcribe_mod.transcriber_id()
    return installed is not None and rec.get("transcriber") != installed


def counts(label: str, conf: float, speech: bool) -> bool:
    """Whether one window's label is a sound to show: CUTOFF or more, or in
    a window with speech, the bars for sound under a voice."""
    if label in SKIP:
        return False
    if not speech:
        return conf >= CUTOFF
    if label in VOICE_LIKE:
        return False
    return conf >= (MASKED_MUSIC if label == "music" else UNDER_SPEECH)


def spans(windows: list[dict]) -> list[dict]:
    """Each kind of sound that counts as spans of time, in order."""
    out: list[dict] = []
    current: dict[str, dict] = {}
    for w in sorted(windows, key=lambda w: w["start"]):
        speech = w["labels"].get("speech", 0.0) >= SPEECH
        for label, conf in w["labels"].items():
            if not counts(label, conf, speech):
                continue
            s = current.get(label)
            if s is not None and w["start"] - s["end"] <= BRIDGE:
                s["end"] = max(s["end"], w["end"])
                s["peak"] = max(s["peak"], conf)
            else:
                current[label] = s = {"label": label, "start": w["start"], "end": w["end"], "peak": conf}
                out.append(s)
    return out


def rows(windows: list[dict]) -> list[dict]:
    """The digest's lines: spans that start and end within SAME seconds of
    the first one on a line share it, strongest first."""
    lines: list[dict] = []
    for s in sorted(spans(windows), key=lambda s: (s["start"], -s["peak"])):
        row = next((r for r in lines if abs(r["anchor"][0] - s["start"]) <= SAME
                    and abs(r["anchor"][1] - s["end"]) <= SAME), None)
        if row is None:
            lines.append({"anchor": (s["start"], s["end"]), "start": s["start"],
                          "end": s["end"], "labels": [s]})
        else:
            row["start"], row["end"] = min(row["start"], s["start"]), max(row["end"], s["end"])
            row["labels"].append(s)
    for r in lines:
        r["labels"].sort(key=lambda s: -s["peak"])
    return lines


def shown(lines: list[dict], lo: float | None = None,
          hi: float | None = None) -> tuple[list[dict], int]:
    """The lines a digest shows, in time order: those overlapping the window
    [lo, hi] when there is one, at most MAX_LINES (the longest). Returns
    them and how many were left out."""
    if lo is not None:
        lines = [r for r in lines if r["end"] > lo and (hi is None or r["start"] < hi)]
    if len(lines) <= MAX_LINES:
        return lines, 0
    keep = sorted(lines, key=lambda r: (-(r["end"] - r["start"]), -r["labels"][0]["peak"]))[:MAX_LINES]
    return sorted(keep, key=lambda r: r["start"]), len(lines) - MAX_LINES


def label_text(label: str) -> str:
    return label.replace("_", " ")


def load(wd: Path) -> dict | None:
    """sounds.json, or None when there is none (or it is unreadable)."""
    try:
        return read_json(wd / SOUNDS_JSON)
    except Exception:  # noqa: BLE001 — no record, no section
        return None
