"""Shared config and helpers for the Mac-native /watch pipeline.

Every phase script imports from here so binary paths, the cache layout, and the
timestamp/JSON conventions live in exactly one place.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# --- Layout -----------------------------------------------------------------
# scripts/ lives at <repo>/scripts ; binaries at <repo>/bin
SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPTS_DIR.parent
BIN_DIR = REPO_DIR / "bin"

# Bump when the extraction contract changes, to invalidate stale caches.
# 1.1.0: 2s floor cap + perceptual-hash dedup + focused-window extraction.
VERSION_TAG = "1.1.0"

# Per-video work/cache lives under a user cache dir so the skill behaves the
# same no matter which project it's invoked from. Override with WATCH_CACHE_DIR.
CACHE_ROOT = Path(
    os.environ.get("WATCH_CACHE_DIR", Path.home() / ".cache" / "claude-video-mac")
)

# --- Binaries ---------------------------------------------------------------
# Prefer the bundled native arm64 builds; fall back to PATH so the pipeline
# still runs on a machine where setup.py hasn't fetched them yet.
def _resolve(name: str) -> str:
    bundled = BIN_DIR / name
    if bundled.exists():
        return str(bundled)
    found = shutil.which(name)
    if found:
        return found
    return str(bundled)  # report the bundled path in errors even if missing


FFMPEG = _resolve("ffmpeg")
FFPROBE = _resolve("ffprobe")
TRANSCRIBE = _resolve("transcribe")  # the Swift CLI, built by setup.py
YTDLP = shutil.which("yt-dlp") or f"{sys.executable} -m yt_dlp"


# --- Process helpers --------------------------------------------------------
def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    """Run a command, capturing output, raising with stderr on failure."""
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    proc = subprocess.run(cmd, **kw)
    if proc.returncode != 0:
        raise RuntimeError(
            f"command failed ({proc.returncode}): {' '.join(cmd[:4])}...\n"
            f"{(proc.stderr or '')[-2000:]}"
        )
    return proc


def log(msg: str) -> None:
    """Progress to stderr so stdout stays a clean machine-readable channel."""
    print(f"[watch] {msg}", file=sys.stderr, flush=True)


# --- Timestamp conventions --------------------------------------------------
def fmt_ts(seconds: float) -> str:
    """Seconds -> MM:SS (or H:MM:SS past an hour). Matches frame tag t=MM:SS."""
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def fmt_vtt_ts(seconds: float) -> str:
    """Seconds -> HH:MM:SS.mmm for WebVTT cues."""
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def parse_ts(value) -> float:
    """Parse a timestamp into seconds. Accepts 'SS', 'MM:SS', 'HH:MM:SS'
    (optional fractional seconds), or a bare number. Used for --start/--end."""
    if value is None:
        raise ValueError("empty timestamp")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        raise ValueError("empty timestamp")
    parts = text.split(":")
    if len(parts) > 3:
        raise ValueError(f"bad timestamp: {value!r}")
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + float(part)
    return seconds


# --- Cache identity ---------------------------------------------------------
def video_id_for(source: str) -> str:
    """Stable cache key for a source.

    URLs: ask yt-dlp for the canonical id (so the same video from different
    query strings collapses to one cache entry). Local files: hash the absolute
    path + size + mtime so edits invalidate naturally.
    """
    p = Path(source)
    if p.exists():
        st = p.stat()
        h = hashlib.sha1(
            f"{p.resolve()}|{st.st_size}|{int(st.st_mtime)}".encode()
        ).hexdigest()[:16]
        return f"local_{h}"
    # URL path: try yt-dlp's id, else hash the URL string
    try:
        out = run([*YTDLP.split(), "--no-warnings", "--print", "id", "--skip-download", source]).stdout.strip()
        if out:
            safe = re.sub(r"[^A-Za-z0-9_-]", "_", out.splitlines()[-1])[:40]
            return f"url_{safe}"
    except Exception:
        pass
    return "url_" + hashlib.sha1(source.encode()).hexdigest()[:16]


def work_dir(video_id: str, create: bool = True) -> Path:
    d = CACHE_ROOT / video_id
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False))


def read_json(path: Path):
    return json.loads(path.read_text())
