"""setup.py — one-time install/preflight for the Mac-native /watch skill.

Idempotent. Safe to re-run. Does:
  1. Environment preflight (macOS 26+, Apple Silicon, Python 3.11+).
  2. pip install the Python deps (pyobjc Vision/Quartz, yt-dlp) if missing.
  3. Fetch native arm64 ffmpeg + ffprobe into bin/ (verified by SHA-256).
  4. Build the Swift SpeechTranscriber CLI into bin/transcribe.
  5. Confirm VideoToolbox is available and the transcriber links.

Run:  python3 setup.py        (full)
      python3 setup.py --check (preflight only, no install)
"""
from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPTS_DIR.parent
BIN_DIR = SKILL_DIR / "bin"
SWIFT_SRC = SCRIPTS_DIR / "transcribe-swift" / "main.swift"

# Native arm64 static builds (osxexperts.net). Pinned hashes = supply-chain
# integrity; if the upstream build rotates, setup fails loudly and the skill
# maintainer re-pins after review. Override with WATCH_FFMPEG_SKIP_HASH=1.
FFMPEG_URL = "https://www.osxexperts.net/ffmpeg81arm.zip"
FFPROBE_URL = "https://www.osxexperts.net/ffprobe81arm.zip"
FFMPEG_SHA = "ebb82529562b71170807bbc6b0e7eb4f0b13af8cbb0e085bb9e8f6fe709598ad"
FFPROBE_SHA = "a6640a77d38a6f0527c5b597e599cb36a3427a6931444ed80bc62542421950a1"

GREEN, RED, YEL, DIM, RST = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def ok(m): print(f"{GREEN}✓{RST} {m}")
def warn(m): print(f"{YEL}!{RST} {m}")
def bad(m): print(f"{RED}✗{RST} {m}")
def step(m): print(f"\n{DIM}== {m} =={RST}")


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


# --- 1. preflight ----------------------------------------------------------
def preflight() -> bool:
    step("Preflight")
    good = True

    mac = platform.mac_ver()[0] or "0"
    major = int(mac.split(".")[0]) if mac[0].isdigit() else 0
    if major >= 26:
        ok(f"macOS {mac} (SpeechAnalyzer + Vision available)")
    else:
        bad(f"macOS {mac} — this skill needs macOS 26 (Tahoe) or newer")
        good = False

    if platform.machine() == "arm64":
        ok("Apple Silicon (arm64)")
    else:
        bad(f"{platform.machine()} — Apple Silicon required")
        good = False

    v = sys.version_info
    if (v.major, v.minor) >= (3, 11):
        ok(f"Python {v.major}.{v.minor}.{v.micro}")
    else:
        bad(f"Python {v.major}.{v.minor} — need 3.11+")
        good = False

    if shutil.which("swift"):
        ver = sh(["swift", "--version"]).stdout.splitlines()[0] if shutil.which("swift") else ""
        ok(f"Swift toolchain ({ver.strip()})")
    else:
        bad("swift not found — install Xcode or Command Line Tools")
        good = False

    return good


# --- 2. python deps --------------------------------------------------------
def py_deps(check_only: bool) -> bool:
    step("Python dependencies")
    needed = {
        "Vision": "pyobjc-framework-Vision",
        "Quartz": "pyobjc-framework-Quartz",
        "yt_dlp": "yt-dlp",
    }
    missing = []
    for mod, pkg in needed.items():
        try:
            __import__(mod)
            ok(f"{pkg}")
        except ImportError:
            missing.append(pkg)
            warn(f"{pkg} missing")
    if missing and not check_only:
        print(f"  installing: {', '.join(missing)}")
        r = sh([sys.executable, "-m", "pip", "install", "--upgrade", *missing])
        if r.returncode != 0 and "externally-managed-environment" in (r.stderr + r.stdout):
            # PEP 668 (Homebrew Python): the interpreter refuses bare installs.
            # Install into the user site instead, which keeps the Homebrew
            # cellar untouched but is still importable by this interpreter.
            warn("PEP 668 environment detected; retrying with --user --break-system-packages")
            r = sh([sys.executable, "-m", "pip", "install", "--upgrade",
                    "--user", "--break-system-packages", *missing])
        if r.returncode != 0:
            bad(f"pip install failed:\n{r.stderr[-500:]}\n"
                f"   consider a venv: python3 -m venv .venv && .venv/bin/python setup.py")
            return False
        ok("installed")
    return not (missing and check_only)


# --- 3. ffmpeg/ffprobe -----------------------------------------------------
def _sha256(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _download(url: str, dest: Path, hash_pinned: bool) -> None:
    """Fetch url -> dest, surviving SSL-verification failures.

    python.org Python installs ship their own OpenSSL and no CA bundle until
    "Install Certificates.command" is run, so the default context can fail with
    CERTIFICATE_VERIFY_FAILED. Fall back to certifi's CA bundle if available,
    then — ONLY when the artifact is SHA-256-pinned (the pin still guarantees
    integrity, so TLS is just transport) — to an unverified connection.
    """
    import ssl

    try:
        urllib.request.urlretrieve(url, dest)
        return
    except urllib.error.URLError as e:
        if not isinstance(getattr(e, "reason", None), ssl.SSLError):
            raise RuntimeError(f"download failed: {e}") from e
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
        with urllib.request.urlopen(url, context=ctx) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f)
        warn("default SSL certs unavailable; used certifi's CA bundle")
        return
    except ImportError:
        pass
    except urllib.error.URLError:
        pass
    if not hash_pinned:
        raise RuntimeError(
            "SSL verification failed and the download is not hash-pinned "
            "(WATCH_FFMPEG_SKIP_HASH=1) — refusing an unverified fetch. Fix the "
            'certs (run "Install Certificates.command" in your Python folder) '
            "or drop the hash bypass."
        )
    warn("SSL verification unavailable; fetching unverified (SHA-256 pin still enforced)")
    ctx = ssl._create_unverified_context()  # noqa: S323 — integrity via pinned SHA
    with urllib.request.urlopen(url, context=ctx) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)


def _fetch_binary(url: str, sha: str, name: str) -> bool:
    import os
    dest = BIN_DIR / name
    if dest.exists():
        r = sh([str(dest), "-version"])
        if r.returncode == 0:
            ok(f"{name} present ({r.stdout.splitlines()[0].split()[2]})")
            return True
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    zpath = BIN_DIR / f"{name}.zip"
    print(f"  downloading {name}…")
    try:
        _download(url, zpath, hash_pinned=os.environ.get("WATCH_FFMPEG_SKIP_HASH") != "1")
    except Exception as e:  # noqa: BLE001 — report, don't traceback
        bad(f"{name} download failed: {e}")
        zpath.unlink(missing_ok=True)
        return False
    got = _sha256(zpath)
    if got != sha and os.environ.get("WATCH_FFMPEG_SKIP_HASH") != "1":
        bad(f"{name} SHA mismatch\n   expected {sha}\n   got      {got}\n"
            f"   upstream build may have rotated; review + re-pin in setup.py "
            f"(or set WATCH_FFMPEG_SKIP_HASH=1 to bypass)")
        zpath.unlink(missing_ok=True)
        return False
    with zipfile.ZipFile(zpath) as z:
        z.extract(name, BIN_DIR)
    zpath.unlink(missing_ok=True)
    dest.chmod(0o755)
    sh(["xattr", "-d", "com.apple.quarantine", str(dest)])
    sh(["codesign", "--force", "--sign", "-", str(dest)])
    r = sh([str(dest), "-version"])
    if r.returncode != 0:
        bad(f"{name} fails to run:\n{r.stderr[-300:]}")
        return False
    ok(f"{name} installed ({r.stdout.splitlines()[0].split()[2]})")
    return True


def ffmpeg_stack(check_only: bool) -> bool:
    step("Native arm64 ffmpeg / ffprobe")
    if check_only:
        good = True
        for n in ("ffmpeg", "ffprobe"):
            p = BIN_DIR / n
            (ok if p.exists() else warn)(f"{n} {'present' if p.exists() else 'not fetched yet'}")
            good = good and p.exists()
        return good
    if not _fetch_binary(FFMPEG_URL, FFMPEG_SHA, "ffmpeg"):
        return False
    if not _fetch_binary(FFPROBE_URL, FFPROBE_SHA, "ffprobe"):
        return False
    # confirm VideoToolbox
    r = sh([str(BIN_DIR / "ffmpeg"), "-hide_banner", "-hwaccels"])
    if "videotoolbox" in r.stdout:
        ok("VideoToolbox hwaccel available")
        return True
    bad("VideoToolbox not reported by ffmpeg")
    return False


# --- 4. swift transcriber --------------------------------------------------
def build_transcriber(check_only: bool) -> bool:
    step("Swift SpeechTranscriber CLI")
    dest = BIN_DIR / "transcribe"
    if check_only:
        (ok if dest.exists() else warn)(f"transcribe {'built' if dest.exists() else 'not built yet'}")
        return dest.exists()
    if not SWIFT_SRC.exists():
        bad(f"missing source: {SWIFT_SRC}")
        return False
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    print("  compiling (swiftc -O)…")
    r = sh(["swiftc", "-O", str(SWIFT_SRC), "-o", str(dest)])
    if r.returncode != 0:
        bad(f"swift build failed:\n{r.stderr[-800:]}")
        return False
    dest.chmod(0o755)
    sh(["codesign", "--force", "--sign", "-", str(dest)])
    ok("transcribe built")
    return True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="preflight only; install nothing")
    args = ap.parse_args()

    print(f"{DIM}Mac-native /watch — setup{RST}")
    pre = preflight()
    if not pre and args.check:
        sys.exit(1)
    if not pre:
        bad("Preflight failed; aborting install.")
        sys.exit(1)

    results = [
        py_deps(args.check),
        ffmpeg_stack(args.check),
        build_transcriber(args.check),
    ]
    step("Result")
    if all(results):
        ok("Ready." if not args.check else "All components present.")
        sys.exit(0)
    bad("Some components are missing — see above.")
    sys.exit(1)


if __name__ == "__main__":
    main()
