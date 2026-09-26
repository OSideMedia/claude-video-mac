"""Shared config and helpers for the Mac-native /watch pipeline.

Every phase script imports from here so binary paths, the cache layout, and the
timestamp/JSON conventions live in exactly one place.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

# --- Layout -----------------------------------------------------------------
# scripts/ lives at <skill>/scripts ; legacy binaries at <skill>/bin
SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPTS_DIR.parent
BIN_DIR = REPO_DIR / "bin"  # legacy per-install location (pre-1.3.0)

# Native binaries live OUTSIDE the plugin install so they survive plugin
# updates (each update gets a fresh versioned dir). Deliberately independent
# of WATCH_CACHE_DIR: relocating the per-video data cache must not orphan the
# binaries. Override with WATCH_BIN_DIR.
SHARED_BIN_DIR = Path(
    os.environ.get("WATCH_BIN_DIR", Path.home() / ".cache" / "claude-video-mac" / "bin")
)

# Bump when the extraction contract changes, to invalidate stale caches.
# 1.2.0: per-window artifact dirs + audio-only support + locale-aware OCR.
# 1.4.0: labeled contact sheets (sheets/, sheets.json) in the digest.
# 1.5.0: council-audit fixes — coverage stats, windowed transcript, normalized
#        floor in the cache key, endpoint-preserving thinning.
VERSION_TAG = "1.5.0"

# Per-video work/cache lives under a user cache dir so the skill behaves the
# same no matter which project it's invoked from. Override with WATCH_CACHE_DIR.
CACHE_ROOT = Path(
    os.environ.get("WATCH_CACHE_DIR", Path.home() / ".cache" / "claude-video-mac")
)

# --- Binaries ---------------------------------------------------------------
# Prefer the shared native arm64 builds (survive plugin updates), then a
# legacy in-install bin/, then PATH so the pipeline still runs on a machine
# where setup.py hasn't fetched them yet.
def _resolve(name: str) -> str:
    for cand in (SHARED_BIN_DIR / name, BIN_DIR / name):
        if cand.exists():
            return str(cand)
    found = shutil.which(name)
    if found:
        return found
    return str(SHARED_BIN_DIR / name)  # report the expected path in errors


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


@contextmanager
def video_lock(wd: Path):
    """Exclusive per-video lock so two concurrent runs on the same video can't
    delete/rename each other's frames or interleave writes to shared files
    (source media, audio wav, transcript). A second run waits, then typically
    lands on the first run's cache."""
    lf = open(wd / ".lock", "w")
    try:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log("another /watch run holds this video; waiting for it to finish…")
            fcntl.flock(lf, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lf, fcntl.LOCK_UN)
        lf.close()


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


# Unsigned decimal only: float() would also accept 'nan', 'inf', '1e5' and a
# leading '-', so '1:-30' used to parse to 30s and 'nan' sailed into ffmpeg.
_TS_PART_RE = re.compile(r"^\d+(?:\.\d+)?$")


def parse_ts(value) -> float:
    """Parse a timestamp into seconds. Accepts 'SS', 'MM:SS', 'HH:MM:SS'
    (optional fractional seconds), or a bare non-negative finite number.
    Used for --start/--end."""
    if value is None:
        raise ValueError("empty timestamp")
    if isinstance(value, bool):
        raise ValueError(f"bad timestamp: {value!r}")
    if isinstance(value, (int, float)):
        f = float(value)
        if not math.isfinite(f) or f < 0:
            raise ValueError(f"bad timestamp: {value!r} (must be finite and >= 0)")
        return f
    text = str(value).strip()
    if not text:
        raise ValueError("empty timestamp")
    parts = text.split(":")
    if len(parts) > 3 or any(not _TS_PART_RE.match(p) for p in parts):
        raise ValueError(f"bad timestamp: {value!r} (use SS, MM:SS or HH:MM:SS)")
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + float(part)
    return seconds


# --- Locale -----------------------------------------------------------------
# language (2-3 letters) [-script (4 letters)] [-region (2 letters | 3 digits)]
_LOCALE_RE = re.compile(r"^([A-Za-z]{2,3})(?:[-_]([A-Za-z]{4}))?(?:[-_]([A-Za-z]{2}|\d{3}))?$")


def normalize_locale(raw) -> str:
    """BCP-47-normalise a --locale: en_US / en-us / EN-US -> en-US,
    zh-hans-cn -> zh-Hans-CN. The value enters the cache key and both
    frameworks, so en_US and en-us must not fork two cache entries."""
    text = str(raw or "").strip()
    m = _LOCALE_RE.match(text)
    if not m:
        raise ValueError(
            f"bad locale {raw!r}: use a BCP-47 tag such as en-US, fr-FR or zh-Hans-CN"
        )
    lang, script, region = m.groups()
    parts = [lang.lower()]
    if script:
        parts.append(script.title())
    if region:
        parts.append(region.upper())
    return "-".join(parts)


def locale_matches(locale: str, supported) -> bool:
    """Is `locale` covered by a framework's supported list? Exact after
    normalisation, or one is a prefix of the other on a tag boundary
    (zh-Hans covers zh-Hans-CN; a bare 'en' request is covered by en-US)."""
    try:
        want = normalize_locale(locale)
    except ValueError:
        return False
    for s in supported or ():
        try:
            have = normalize_locale(s)
        except ValueError:
            continue
        if have == want or want.startswith(have + "-") or have.startswith(want + "-"):
            return True
    return False


# --- Source resolution ------------------------------------------------------
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}
AUDIO_EXTS = {".m4a", ".mp3", ".wav", ".aiff", ".aac", ".flac", ".ogg"}
MEDIA_EXTS = VIDEO_EXTS | AUDIO_EXTS


def is_bare_playlist_url(url: str) -> bool:
    """A playlist URL with no single video in it (`list=` but no `v=`, or a
    /playlist path). yt-dlp would download the FIRST entry while `--print`
    reported the LAST, so the cache key and the media disagreed; refuse
    instead of guessing which entry the user meant."""
    from urllib.parse import parse_qs, urlsplit

    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if "://" not in url:
        return False
    qs = parse_qs(parts.query)
    if parts.path.rstrip("/").endswith("/playlist"):
        return True
    # youtu.be/<id>?list=… and /embed/<id>?list=… carry the video in the path
    return "list" in qs and "v" not in qs and parts.path.rstrip("/") in ("", "/watch")


def resolve_source(source: str) -> str:
    """Normalize a source before the pipeline sees it.

    Local paths: expand ~, resolve to absolute (so the cache id is identical no
    matter how the path was spelled). A directory containing exactly one media
    file resolves to that file; otherwise the caller gets a list to pick from.
    Anything that doesn't exist on disk is passed through as a URL, except a
    bare playlist URL, which is refused.
    """
    p = Path(source).expanduser()
    if not p.exists():
        if is_bare_playlist_url(source):
            raise ValueError(
                f"{source} is a playlist, not a single video — pass one video's "
                "URL (with v=) instead"
            )
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
# A plaintext history: every URL ever resolved, mapped to its cache id. Kept so
# cached follow-ups skip the network; `--purge` drops a URL's entry and
# `--purge-history` clears the file.
URL_ID_MAP = CACHE_ROOT / "url_ids.json"

# Common yt-dlp switches for every invocation. --playlist-items 1 pins the
# single entry a multi-entry URL would expand to, so `--print` (one line per
# entry) and `-o source.%(ext)s` (first entry) can never disagree.
YTDLP_COMMON = ["--no-warnings", "--no-playlist", "--playlist-items", "1"]


def ytdlp_id_argv(source: str) -> list[str]:
    """argv for the one metadata call that resolves a URL's cache id. `--`
    keeps a source starting with '-' from being read as an option."""
    return [*YTDLP, *YTDLP_COMMON,
            "--print", "%(extractor_key)s.%(id)s",
            "--skip-download", "--", source]


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
        # st_mtime_ns: full timestamp precision, so a same-size rewrite within
        # the same second still invalidates the cache entry.
        h = hashlib.sha1(
            f"{p.resolve()}|{st.st_size}|{st.st_mtime_ns}".encode()
        ).hexdigest()[:16]
        return f"local_{h}"
    # Known URL? Use the persisted id — no network.
    try:
        mapping = read_json(URL_ID_MAP)
    except Exception:
        mapping = {}
    if source in mapping:
        return mapping[source]
    # URL path: try yt-dlp's extractor+id (video ids are only unique per
    # extractor, so the id alone could collide across sites), else hash the URL.
    try:
        out = run(ytdlp_id_argv(source)).stdout.strip()
        if out:
            safe = re.sub(r"[^A-Za-z0-9_-]", "_", out.splitlines()[-1])[:56]
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


def write_text_atomic(path: Path, text: str) -> None:
    """Temp + rename: a run killed mid-write never leaves a truncated file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_json(path: Path, obj) -> None:
    """Atomic: a run killed mid-write must never leave a truncated JSON file
    (a corrupt done.json/frames.json would otherwise poison later runs)."""
    write_text_atomic(path, json.dumps(obj, indent=2, ensure_ascii=False))


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))
