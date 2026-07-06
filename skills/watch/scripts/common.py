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
# 1.2.0: per-window artifact dirs + audio-only support + locale-aware OCR.
VERSION_TAG = "1.2.0"

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
# argv prefix, never a string: paths (e.g. sys.executable) may contain spaces
_ytdlp_bin = shutil.which("yt-dlp")
YTDLP: list[str] = [_ytdlp_bin] if _ytdlp_bin else [sys.executable, "-m", "yt_dlp"]


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


# --- Source resolution ------------------------------------------------------
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}
AUDIO_EXTS = {".m4a", ".mp3", ".wav", ".aiff", ".aac", ".flac", ".ogg"}
MEDIA_EXTS = VIDEO_EXTS | AUDIO_EXTS


def resolve_source(source: str) -> str:
    """Normalize a source before the pipeline sees it.

    Local paths: expand ~, resolve to absolute (so the cache id is identical no
    matter how the path was spelled). A directory containing exactly one media
    file resolves to that file; otherwise the caller gets a list to pick from.
    Anything that doesn't exist on disk is passed through as a URL.
    """
    p = Path(source).expanduser()
    if not p.exists():
        return source  # URL (or a typo'd path — yt-dlp will say so)
    p = p.resolve()
    if p.is_dir():
        media = sorted(f for f in p.iterdir() if f.suffix.lower() in MEDIA_EXTS)
        if not media:
            raise ValueError(f"{p} is a directory with no video/audio files")
        if len(media) > 1:
            names = "\n  ".join(f.name for f in media[:20])
            raise ValueError(
                f"{p} contains {len(media)} media files — specify one:\n  {names}"
            )
        log(f"directory given; using {media[0].name}")
        p = media[0]
    return str(p)


# --- Cache identity ---------------------------------------------------------
URL_ID_MAP = CACHE_ROOT / "url_ids.json"


def video_id_for(source: str) -> str:
    """Stable cache key for a source.

    URLs: ask yt-dlp for the canonical id (so the same video from different
    query strings collapses to one cache entry). Resolved ids are persisted in
    URL_ID_MAP so cached follow-ups never pay a network round-trip — and a
    transient rate-limit can't flip the key to the hash fallback and silently
    orphan the cache. Local files: hash the absolute path + size + mtime so
    edits invalidate naturally.
    """
    p = Path(source)
    if p.exists():
        st = p.stat()
        h = hashlib.sha1(
            f"{p.resolve()}|{st.st_size}|{int(st.st_mtime)}".encode()
        ).hexdigest()[:16]
        return f"local_{h}"
    # Known URL? Use the persisted id — no network.
    try:
        mapping = read_json(URL_ID_MAP)
    except Exception:
        mapping = {}
    if source in mapping:
        return mapping[source]
    # URL path: try yt-dlp's id, else hash the URL string
    try:
        out = run([*YTDLP, "--no-warnings", "--no-playlist", "--print", "id",
                   "--skip-download", source]).stdout.strip()
        if out:
            safe = re.sub(r"[^A-Za-z0-9_-]", "_", out.splitlines()[-1])[:40]
            vid = f"url_{safe}"
            # Persist only real ids: the hash fallback must never stick, or a
            # one-off failure would pin this URL to the wrong cache key forever.
            try:
                mapping[source] = vid
                CACHE_ROOT.mkdir(parents=True, exist_ok=True)
                write_json(URL_ID_MAP, mapping)
            except Exception:
                pass
            return vid
    except Exception:
        pass
    return "url_" + hashlib.sha1(source.encode()).hexdigest()[:16]


def work_dir(video_id: str, create: bool = True) -> Path:
    d = CACHE_ROOT / video_id
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def artifact_dir(wd: Path, start: float | None, end: float | None, create: bool = True) -> Path:
    """Where a run's frames/OCR/digest live. Full-video runs use the work dir
    itself; focused-window runs get their own namespace so they never clobber
    the full-video artifacts (or each other)."""
    if start is None and end is None:
        return wd
    s = f"{start:.2f}" if start is not None else "0"
    e = f"{end:.2f}" if end is not None else "end"
    d = wd / "windows" / f"{s}-{e}"
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def cache_size_bytes() -> int:
    total = 0
    if CACHE_ROOT.exists():
        for p in CACHE_ROOT.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                pass
    return total


def write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False))


def read_json(path: Path):
    return json.loads(path.read_text())
