"""doctor — environment report for the Mac-native /watch skill.

    python3 watch.py doctor

Prints what the pipeline will ACTUALLY use — the interpreter, each resolved
binary (path / architecture / version), VideoToolbox, pyobjc Vision/Quartz,
the SpeechTranscriber locales and Vision OCR languages, the cache split into
media vs binaries, the URL-history size and the bin dir in use — and exits 1
when a prerequisite is missing. Reuses setup.py's probes; every probe is
guarded so one failing check cannot hide the rest of the report.
"""
from __future__ import annotations

import importlib.util
import json
import platform
import subprocess
import sys
from pathlib import Path

import setup as setup_mod
from common import (
    CACHE_ROOT,
    LEGACY_SHARED_BIN_DIR,
    SHARED_BIN_DIR,
    URL_ID_MAP,
    VERSION_TAG,
    cache_size_bytes,
    dir_size_bytes,
    read_json,
)

BINARIES = ("ffmpeg", "ffprobe", "transcribe")


# --- probes (each swappable, so the report logic is unit-testable) -----------
def _probe_binary(name: str) -> dict | None:
    path = setup_mod._resolved(name)
    if not path:
        return None
    archs = setup_mod.binary_archs(path)
    if name == "transcribe":
        r = setup_mod.sh([path, "--locales"])
        version = "1.6.0+ (--locales)" if r.returncode == 0 else "pre-1.6.0 (no --locales; re-run setup.py)"
    else:
        r = setup_mod.sh([path, "-version"])
        version = setup_mod._version_word(r.stdout) if r.returncode == 0 else "does not run"
    return {"path": path, "archs": archs, "native": platform.machine() in archs, "version": version}


def _probe_videotoolbox(ffmpeg_path: str | None) -> bool:
    if not ffmpeg_path:
        return False
    r = setup_mod.sh([ffmpeg_path, "-hide_banner", "-hwaccels"])
    return "videotoolbox" in (r.stdout or "")


def _probe_pyobjc() -> dict:
    return {m: importlib.util.find_spec(m) is not None for m in ("Vision", "Quartz")}


def _probe_speech_locales() -> list[str] | None:
    path = setup_mod._resolved("transcribe")
    if not path:
        return None
    try:
        r = subprocess.run([path, "--locales"], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return None
        data = json.loads(r.stdout)
        return [str(x) for x in data] if isinstance(data, list) else None
    except Exception:  # noqa: BLE001
        return None


def _probe_vision_languages() -> list[str] | None:
    try:
        import ocr  # lazy: needs pyobjc
        return ocr.supported_languages()
    except Exception:  # noqa: BLE001
        return None


def _probe_cache() -> dict:
    ffmpeg = setup_mod._resolved("ffmpeg")
    bin_dir = Path(ffmpeg).parent if ffmpeg else SHARED_BIN_DIR
    urls = 0
    try:
        urls = len(read_json(URL_ID_MAP))
    except Exception:  # noqa: BLE001 — no history file
        pass
    legacy_present = any((LEGACY_SHARED_BIN_DIR / n).exists() for n in BINARIES)
    try:
        in_legacy = bin_dir.resolve() == LEGACY_SHARED_BIN_DIR.resolve()
    except OSError:
        in_legacy = False
    return {
        "root": str(CACHE_ROOT),
        "media_bytes": cache_size_bytes(),
        "bin_dir": str(bin_dir),
        "bin_bytes": dir_size_bytes(bin_dir),
        "url_ids": urls,
        "legacy_bin_present": legacy_present and not in_legacy,
        "bin_dir_is_legacy": in_legacy,
    }


DEFAULT_PROBES = {
    "macos": lambda: platform.mac_ver()[0] or "",
    "machine": platform.machine,
    "binary": _probe_binary,
    "videotoolbox": _probe_videotoolbox,
    "pyobjc": _probe_pyobjc,
    "speech_locales": _probe_speech_locales,
    "vision_languages": _probe_vision_languages,
    "cache": _probe_cache,
}


# --- report ------------------------------------------------------------------
def collect(probes: dict | None = None) -> dict:
    p = dict(DEFAULT_PROBES)
    p.update(probes or {})
    rep: dict = {"version": VERSION_TAG, "interpreter": sys.executable,
                 "python": sys.version.split()[0], "problems": [], "warnings": []}
    problems, warnings = rep["problems"], rep["warnings"]

    def guard(name, fn, *args, default=None):
        try:
            return fn(*args)
        except Exception as e:  # noqa: BLE001 — one broken probe must not hide the rest
            warnings.append(f"{name} probe failed: {type(e).__name__}: {e}")
            return default

    rep["macos"] = guard("macos", p["macos"], default="") or ""
    rep["machine"] = guard("machine", p["machine"], default="") or ""
    major = int(rep["macos"].split(".")[0]) if rep["macos"][:1].isdigit() else 0
    if major < 26:
        problems.append(f"macOS {rep['macos'] or 'unknown'}: this skill needs macOS 26 (Tahoe) or newer")
    if rep["machine"] != "arm64":
        problems.append(f"{rep['machine'] or 'unknown arch'}: Apple Silicon (arm64) required")

    rep["binaries"] = {}
    for name in BINARIES:
        info = guard(name, p["binary"], name)
        rep["binaries"][name] = info
        if not info:
            problems.append(f"{name} not found in any bin dir or PATH — run setup.py")
        elif not info.get("native"):
            archs = ", ".join(info.get("archs") or []) or "not a native executable"
            problems.append(f"{name} at {info['path']} is not a {rep['machine']} binary ({archs})")

    ffmpeg_path = (rep["binaries"].get("ffmpeg") or {}).get("path")
    rep["videotoolbox"] = bool(guard("videotoolbox", p["videotoolbox"], ffmpeg_path, default=False))
    if ffmpeg_path and not rep["videotoolbox"]:
        problems.append("VideoToolbox hwaccel not reported by ffmpeg (not the native arm64 build?)")

    rep["pyobjc"] = guard("pyobjc", p["pyobjc"], default={}) or {}
    missing = [f"pyobjc-framework-{m}" for m, present in rep["pyobjc"].items() if not present]
    if missing:
        problems.append(f"{', '.join(missing)} not importable by {sys.executable}; "
                        f'fix: "{sys.executable}" -m pip install {" ".join(missing)}')

    rep["speech_locales"] = guard("speech_locales", p["speech_locales"])
    if rep["speech_locales"] is None:
        warnings.append("speech locales unavailable: transcribe predates 1.6.0 (no --locales) "
                        "or failed to run — re-run setup.py to rebuild it")
    rep["vision_languages"] = guard("vision_languages", p["vision_languages"])
    if rep["vision_languages"] is None:
        warnings.append("Vision languages unavailable (pyobjc Vision not importable here)")

    rep["cache"] = guard("cache", p["cache"], default={}) or {}
    if rep["cache"].get("legacy_bin_present"):
        warnings.append(f"binaries also present in the legacy dir {LEGACY_SHARED_BIN_DIR}; "
                        "setup.py moves them into the bin dir in use")
    if rep["cache"].get("bin_dir_is_legacy"):
        warnings.append(f"binaries live in the pre-1.6.0 dir inside the cache ({LEGACY_SHARED_BIN_DIR}); "
                        "re-run setup.py to move them to ~/.local/share/claude-video-mac/bin")
    return rep


def is_healthy(rep: dict) -> bool:
    return not rep["problems"]


def _size(n) -> str:
    n = float(n or 0)
    return f"{n / 1e9:.2f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


def _some(items, n=8) -> str:
    items = list(items or [])
    shown = ", ".join(items[:n])
    return shown + (f", … ({len(items)} total)" if len(items) > n else "")


def render(rep: dict) -> str:
    L: list[str] = []
    a = L.append
    a(f"claude-video-mac doctor  (skill {rep['version']})")
    a(f"interpreter:      {rep['interpreter']}  (Python {rep['python']})")
    a(f"system:           macOS {rep.get('macos') or '?'}  {rep.get('machine') or '?'}")
    cache = rep.get("cache") or {}
    a(f"bin dir in use:   {cache.get('bin_dir', '?')}  ({_size(cache.get('bin_bytes'))})")
    for name in BINARIES:
        info = (rep.get("binaries") or {}).get(name)
        if info:
            archs = ", ".join(info.get("archs") or []) or "no arch"
            a(f"  {name:<11} {info['path']}  ({archs}; {info.get('version')})")
        else:
            a(f"  {name:<11} MISSING")
    a(f"VideoToolbox:     {'yes' if rep.get('videotoolbox') else 'no'}")
    pyobjc = rep.get("pyobjc") or {}
    a("pyobjc:           " + ", ".join(f"{m} {'ok' if v else 'MISSING'}" for m, v in pyobjc.items()))
    sl = rep.get("speech_locales")
    a(f"speech locales:   {len(sl)} — {_some(sl)}" if sl is not None else "speech locales:   unavailable")
    vl = rep.get("vision_languages")
    a(f"Vision languages: {len(vl)} — {_some(vl)}" if vl is not None else "Vision languages: unavailable")
    a(f"cache:            {cache.get('root', '?')}  media {_size(cache.get('media_bytes'))}, "
      f"bin {_size(cache.get('bin_bytes'))}")
    a(f"url_ids.json:     {cache.get('url_ids', 0)} URL(s) in the history (--purge-history clears it)")
    for w in rep.get("warnings") or []:
        a(f"! {w}")
    for pr in rep.get("problems") or []:
        a(f"x {pr}")
    a("OK — ready to watch" if is_healthy(rep) else "NOT READY — fix the x items above")
    return "\n".join(L)


def exit_code(probes: dict | None = None) -> int:
    rep = collect(probes)
    print(render(rep))
    return 0 if is_healthy(rep) else 1


if __name__ == "__main__":
    sys.exit(exit_code())
