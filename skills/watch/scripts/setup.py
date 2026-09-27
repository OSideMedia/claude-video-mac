"""setup.py — one-time install/preflight for the Mac-native /watch skill.

Idempotent. Safe to re-run. Does:
  1. Environment preflight (macOS 26+, Apple Silicon, Python 3.11+; Swift only
     when the transcriber actually needs (re)building).
  2. pip install the Python deps (pyobjc Vision/Quartz, yt-dlp) if missing.
  3. Fetch native arm64 ffmpeg + ffprobe into the shared bin dir (SHA-256
     verified), MOVING copies from the pre-1.6.0 locations instead of
     re-downloading ~100 MB.
  4. Build the Swift SpeechTranscriber CLI — skipped when main.swift is
     unchanged since the last build — or install a SHA-verified prebuilt one
     (--transcribe-url / --transcribe-sha256, no Xcode needed).
  5. Confirm VideoToolbox is available.

Run:  python3 setup.py         (full)
      python3 setup.py --check (preflight only, no install)
"""
from __future__ import annotations

import argparse
import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPTS_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
from common import (  # noqa: E402 — stdlib-only; ONE source of truth for the dirs
    BIN_DIR as LEGACY_REPO_BIN_DIR,
    DEFAULT_BIN_DIR,
    LEGACY_SHARED_BIN_DIR,
    SHARED_BIN_DIR,
    bin_search_dirs,
)

# Where binaries go: the WATCH_BIN_DIR override, else ~/.local/share/claude-video-mac/bin.
BIN_DIR = SHARED_BIN_DIR
# Where earlier versions put them; migrated (moved) into BIN_DIR when setup runs.
LEGACY_BIN_DIRS = [LEGACY_SHARED_BIN_DIR, LEGACY_REPO_BIN_DIR]
SWIFT_SRC = SCRIPTS_DIR / "transcribe-swift" / "main.swift"
# Sidecars next to the transcribe binary. SRC_HASH_NAME: SHA-256 of the
# main.swift a LOCAL build came from (unchanged source + native binary = no
# rebuild). ASSET_HASH_NAME: SHA-256 of a PREBUILT release asset as installed
# (intact asset = up to date; never forged as "built from this source").
SRC_HASH_NAME = "transcribe.src.sha256"
ASSET_HASH_NAME = "transcribe.asset.sha256"
DOWNLOAD_TIMEOUT = 60  # seconds per socket operation; urlretrieve had none

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


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _version_word(stdout: str) -> str:
    """'ffmpeg version 8.1 Copyright…' -> '8.1'; never IndexErrors."""
    for line in stdout.splitlines():
        words = line.split()
        if len(words) >= 3:
            return words[2]
    return "version unknown"


# --- architecture ------------------------------------------------------------
_ARCH_TOKENS = {"arm64": "arm64", "arm64e": "arm64", "x86_64": "x86_64",
                "x86-64": "x86_64", "aarch64": "aarch64"}


def binary_archs(path) -> list[str]:
    """CPU architectures a native executable contains, via `file -b` (and
    `lipo -archs` for Mach-O). [] for anything that is not a Mach-O/ELF file —
    so a stray shell script or an x86_64 download is never mistaken for a
    working binary just because `-version` happened to exit 0."""
    p = Path(path)
    if not p.is_file():
        return []
    r = sh(["file", "-b", str(p)])
    desc = (r.stdout or "").strip() if r.returncode == 0 else ""
    if not desc.startswith(("Mach-O", "ELF")):
        return []
    if desc.startswith("Mach-O") and shutil.which("lipo"):
        r2 = sh(["lipo", "-archs", str(p)])
        if r2.returncode == 0 and r2.stdout.strip():
            return sorted({_ARCH_TOKENS.get(t, t) for t in r2.stdout.split()})
    found = {_ARCH_TOKENS[t] for t in re.findall(r"[A-Za-z0-9_-]+", desc) if t in _ARCH_TOKENS}
    return sorted(found)


def is_native_binary(path) -> bool:
    return platform.machine() in binary_archs(path)


def binary_runs(path, name: str) -> bool:
    """Does the binary actually execute? ffmpeg/ffprobe: `-version` exits 0.
    transcribe: invoked with no args it exits 2 (usage) — proof it loads and
    runs, for both the pre-1.6.0 and the 1.6.0 CLI."""
    if name == "transcribe":
        return sh([str(path)]).returncode == 2
    return sh([str(path), "-version"]).returncode == 0


# --- 1. preflight ----------------------------------------------------------
def preflight(need_swift: bool = True) -> bool:
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
        ok(f"Python {v.major}.{v.minor}.{v.micro}  ({sys.executable})")
    else:
        bad(f"Python {v.major}.{v.minor} — need 3.11+  ({sys.executable})")
        good = False

    if shutil.which("swift"):
        r = sh(["swift", "--version"])
        # first non-empty line of either stream; a fresh CLT install can print nothing
        lines = [ln for ln in (r.stdout + "\n" + r.stderr).splitlines() if ln.strip()]
        ver = lines[0].strip() if lines else "version unknown"
        ok(f"Swift toolchain ({ver})")
    elif need_swift:
        bad("swift not found — install Xcode or Command Line Tools, or pass "
            "--transcribe-url/--transcribe-sha256 to install a prebuilt transcribe")
        good = False
    else:
        warn("swift not found (not needed: transcribe is up to date or prebuilt)")

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
        print(f"  installing into {sys.executable}: {', '.join(missing)}")
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
    elif missing and check_only:
        print(f'  fix: "{sys.executable}" -m pip install {" ".join(missing)}')
    return not (missing and check_only)


# --- 3. downloads + migration ------------------------------------------------
def _download(url: str, dest: Path, hash_pinned: bool) -> None:
    """Fetch url -> dest with a socket timeout, surviving SSL-verification failures.

    python.org Python installs ship their own OpenSSL and no CA bundle until
    "Install Certificates.command" is run, so the default context can fail with
    CERTIFICATE_VERIFY_FAILED. Fall back to certifi's CA bundle if available,
    then — ONLY when the artifact is SHA-256-pinned (the pin still guarantees
    integrity, so TLS is just transport) — to an unverified connection.
    """
    import ssl

    def fetch(ctx=None) -> None:
        with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT, context=ctx) as r, \
                open(dest, "wb") as f:
            shutil.copyfileobj(r, f)

    try:
        fetch()
        return
    except urllib.error.URLError as e:
        if not isinstance(getattr(e, "reason", None), ssl.SSLError):
            raise RuntimeError(f"download failed: {e}") from e
    except OSError as e:  # read timeout and friends
        raise RuntimeError(f"download failed: {e}") from e
    try:
        import certifi
        fetch(ssl.create_default_context(cafile=certifi.where()))
        warn("default SSL certs unavailable; used certifi's CA bundle")
        return
    except ImportError:
        pass
    except (urllib.error.URLError, OSError):
        pass
    if not hash_pinned:
        raise RuntimeError(
            "SSL verification failed and the download is not hash-pinned "
            "(WATCH_FFMPEG_SKIP_HASH=1) — refusing an unverified fetch. Fix the "
            'certs (run "Install Certificates.command" in your Python folder) '
            "or drop the hash bypass."
        )
    warn("SSL verification unavailable; fetching unverified (SHA-256 pin still enforced)")
    fetch(ssl._create_unverified_context())  # noqa: S323 — integrity via pinned SHA


def migrate_legacy(name: str, dest_dir: Path | None = None,
                   legacy_dirs: list[Path] | None = None) -> Path | None:
    """MOVE a native `name` from a pre-1.6.0 location into dest_dir (first
    legacy dir holding one wins), then remove any other legacy copies once
    dest_dir holds a working one — so the 103 MB in the cache dir and the
    in-repo bin/ stop being counted or shipped twice. Returns the destination
    when a move happened, else None. Non-native or absent copies are left alone."""
    dest_dir = Path(dest_dir or BIN_DIR)
    legacy_dirs = LEGACY_BIN_DIRS if legacy_dirs is None else list(legacy_dirs)
    dest = dest_dir / name
    moved = None
    # "dest is fine" means native AND runs — a native-but-broken dest must not
    # cost the working legacy copy (and a re-download); it gets replaced by it.
    dest_ok = dest.exists() and is_native_binary(dest) and binary_runs(dest, name)
    if not dest_ok:
        for legacy in legacy_dirs:
            cand = Path(legacy) / name
            if cand.exists() and is_native_binary(cand) and binary_runs(cand, name):
                dest_dir.mkdir(parents=True, exist_ok=True)
                if dest.exists():
                    warn(f"{name} at {dest} does not run; replacing it with the working copy from {legacy}")
                    dest.unlink()
                shutil.move(str(cand), str(dest))
                ok(f"{name} migrated: {legacy} -> {dest_dir}")
                moved = dest
                dest_ok = True
                break
    if dest_ok:  # only a VERIFIED destination justifies deleting the other copies
        for legacy in legacy_dirs:
            cand = Path(legacy) / name
            try:
                same = cand.exists() and cand.resolve() == dest.resolve()
            except OSError:
                same = False
            if cand.exists() and not same and is_native_binary(cand):
                cand.unlink()
                ok(f"removed legacy copy {cand}")
    return moved


def _fetch_binary(url: str, sha: str, name: str, bin_dir: Path | None = None,
                  legacy_dirs: list[Path] | None = None) -> bool:
    bin_dir = Path(bin_dir or BIN_DIR)
    dest = bin_dir / name
    migrate_legacy(name, bin_dir, legacy_dirs)
    if dest.exists():
        if not is_native_binary(dest):
            warn(f"{name} at {dest} is not a {platform.machine()} binary "
                 f"({', '.join(binary_archs(dest)) or 'not an executable'}); replacing")
            dest.unlink()
        else:
            r = sh([str(dest), "-version"])
            if r.returncode == 0:
                ok(f"{name} present ({_version_word(r.stdout)}) at {dest}")
                return True
            warn(f"{name} present but fails to run; re-fetching")
    bin_dir.mkdir(parents=True, exist_ok=True)
    zpath = bin_dir / f"{name}.zip"
    skip_hash = os.environ.get("WATCH_FFMPEG_SKIP_HASH") == "1"
    print(f"  downloading {name}…")
    try:
        _download(url, zpath, hash_pinned=not skip_hash)
        got = _sha256(zpath)
        if got != sha and not skip_hash:
            bad(f"{name} SHA mismatch\n   expected {sha}\n   got      {got}\n"
                f"   upstream build may have rotated; review + re-pin in setup.py "
                f"(or set WATCH_FFMPEG_SKIP_HASH=1 to bypass)")
            return False
        with zipfile.ZipFile(zpath) as z:
            z.extract(name, bin_dir)
    except Exception as e:  # noqa: BLE001 — report, don't traceback
        bad(f"{name} fetch failed: {e}")
        return False
    finally:
        zpath.unlink(missing_ok=True)  # never leave a half zip in the bin dir
    dest.chmod(0o755)
    sh(["xattr", "-d", "com.apple.quarantine", str(dest)])
    sh(["codesign", "--force", "--sign", "-", str(dest)])
    if not is_native_binary(dest):
        bad(f"{name} is not a {platform.machine()} binary ({', '.join(binary_archs(dest)) or 'unknown'})")
        return False
    r = sh([str(dest), "-version"])
    if r.returncode != 0:
        bad(f"{name} fails to run:\n{r.stderr[-300:]}")
        return False
    ok(f"{name} installed ({_version_word(r.stdout)}) at {dest}")
    return True


def _resolved(name: str) -> str | None:
    """Mirror common.resolve_binary's search order so --check reports what the
    pipeline will actually use; None (not the expected path) when missing."""
    for d in bin_search_dirs():
        if (d / name).exists():
            return str(d / name)
    return shutil.which(name)


def ffmpeg_stack(check_only: bool) -> bool:
    step("Native arm64 ffmpeg / ffprobe")
    if check_only:
        good = True
        for n in ("ffmpeg", "ffprobe"):
            p = _resolved(n)
            if p:
                archs = ", ".join(binary_archs(p)) or "unknown arch"
                (ok if is_native_binary(p) else warn)(f"{n} {p} ({archs})")
                good = good and is_native_binary(p)
            else:
                warn(f"{n} not fetched yet")
                good = False
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
def _read_side(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def transcriber_up_to_date(bin_dir: Path | None = None) -> bool:
    """A native transcribe exists and EITHER its source sidecar matches the
    current main.swift (local build) OR its asset sidecar matches the binary
    itself (an intact prebuilt release asset; setup never rebuilds over one)."""
    bin_dir = Path(bin_dir or BIN_DIR)
    dest = bin_dir / "transcribe"
    if not (dest.exists() and is_native_binary(dest)):
        return False
    src_side = _read_side(bin_dir / SRC_HASH_NAME)
    if src_side and SWIFT_SRC.exists() and src_side == _sha256(SWIFT_SRC):
        return True
    asset_side = _read_side(bin_dir / ASSET_HASH_NAME)
    return bool(asset_side) and asset_side == _sha256(dest)


def _finish_transcriber(dest: Path, bin_dir: Path, prebuilt: bool = False) -> None:
    dest.chmod(0o755)
    sh(["xattr", "-d", "com.apple.quarantine", str(dest)])
    sh(["codesign", "--force", "--sign", "-", str(dest)])
    # Exactly one sidecar describes the binary: its source (local build) or
    # its installed hash (prebuilt). A stale sidecar of the other kind is removed.
    if prebuilt:
        # Hash the file AS INSTALLED — after the ad-hoc re-sign, which can
        # rewrite the signature bytes (a plain swiftc build does; an asset from
        # release-transcribe.sh survives). Recording the pre-sign hash made the
        # installed binary read as stale and a later setup run demand Swift.
        (bin_dir / ASSET_HASH_NAME).write_text(_sha256(dest) + "\n", encoding="utf-8")
        (bin_dir / SRC_HASH_NAME).unlink(missing_ok=True)
    elif SWIFT_SRC.exists():
        (bin_dir / SRC_HASH_NAME).write_text(_sha256(SWIFT_SRC) + "\n", encoding="utf-8")
        (bin_dir / ASSET_HASH_NAME).unlink(missing_ok=True)


def install_prebuilt_transcriber(url: str, sha256_hex: str, bin_dir: Path | None = None) -> bool:
    """Download a release-asset transcribe, verify its SHA-256 BEFORE it lands
    at the final path, then chmod/codesign it. Removes the Xcode prerequisite."""
    bin_dir = Path(bin_dir or BIN_DIR)
    dest = bin_dir / "transcribe"
    want = (sha256_hex or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", want):
        bad(f"--transcribe-sha256 must be 64 hex chars (got {sha256_hex!r})")
        return False
    bin_dir.mkdir(parents=True, exist_ok=True)
    tmp = bin_dir / "transcribe.download"
    print(f"  downloading prebuilt transcribe from {url}…")
    try:
        _download(url, tmp, hash_pinned=True)
        got = _sha256(tmp)
        if got != want:
            bad(f"transcribe SHA mismatch\n   expected {want}\n   got      {got}\n"
                "   refusing to install; check the release asset and its published hash")
            return False
        os.replace(tmp, dest)
    except Exception as e:  # noqa: BLE001
        bad(f"prebuilt transcribe install failed: {e}")
        return False
    finally:
        tmp.unlink(missing_ok=True)
    _finish_transcriber(dest, bin_dir, prebuilt=True)
    if not is_native_binary(dest):
        bad(f"downloaded transcribe is not a {platform.machine()} binary "
            f"({', '.join(binary_archs(dest)) or 'unknown'})")
        return False
    ok(f"transcribe installed from release asset at {dest}")
    return True


def build_transcriber(check_only: bool, bin_dir: Path | None = None,
                      legacy_dirs: list[Path] | None = None) -> bool:
    step("Swift SpeechTranscriber CLI")
    bin_dir = Path(bin_dir or BIN_DIR)
    dest = bin_dir / "transcribe"
    if check_only:
        p = _resolved("transcribe")
        if p:
            fresh = transcriber_up_to_date(Path(p).parent)
            (ok if fresh else warn)(f"transcribe {p}" + ("" if fresh else
                                    " (main.swift changed or no build record; setup will rebuild)"))
        else:
            warn("transcribe not built yet")
        return bool(p)
    migrate_legacy("transcribe", bin_dir, legacy_dirs)
    if transcriber_up_to_date(bin_dir):
        ok(f"transcribe up to date at {dest} (main.swift unchanged; no rebuild)")
        return True
    if not SWIFT_SRC.exists():
        bad(f"missing source: {SWIFT_SRC}")
        return False
    if not shutil.which("swiftc"):
        bad("swiftc not found — install Xcode or Command Line Tools, or pass "
            "--transcribe-url/--transcribe-sha256 to install a prebuilt binary")
        return False
    bin_dir.mkdir(parents=True, exist_ok=True)
    print("  compiling (swiftc -O)…")
    r = sh(["swiftc", "-O", str(SWIFT_SRC), "-o", str(dest)])
    if r.returncode != 0:
        bad(f"swift build failed:\n{r.stderr[-800:]}")
        return False
    _finish_transcriber(dest, bin_dir)
    ok(f"transcribe built at {dest}")
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="preflight only; install nothing")
    ap.add_argument("--transcribe-url", default=os.environ.get("WATCH_TRANSCRIBE_URL"),
                    help="install a prebuilt transcribe from this URL instead of building "
                         "(env WATCH_TRANSCRIBE_URL); requires --transcribe-sha256")
    ap.add_argument("--transcribe-sha256", default=os.environ.get("WATCH_TRANSCRIBE_SHA256"),
                    help="SHA-256 the prebuilt transcribe must match (env WATCH_TRANSCRIBE_SHA256)")
    args = ap.parse_args()

    prebuilt = bool(args.transcribe_url)
    if prebuilt and not args.transcribe_sha256:
        bad("--transcribe-url needs --transcribe-sha256 (refusing an unverified binary)")
        sys.exit(2)

    print(f"{DIM}Mac-native /watch — setup{RST}")
    print(f"  bin dir: {BIN_DIR}" + ("  (WATCH_BIN_DIR)" if os.environ.get("WATCH_BIN_DIR") else ""))
    need_swift = not args.check and not prebuilt and not transcriber_up_to_date()
    pre = preflight(need_swift=need_swift)
    if not pre and args.check:
        sys.exit(1)
    if not pre:
        bad("Preflight failed; aborting install.")
        sys.exit(1)

    if prebuilt and not args.check:
        transcriber = install_prebuilt_transcriber(args.transcribe_url, args.transcribe_sha256)
    else:
        transcriber = build_transcriber(args.check)
    results = [
        py_deps(args.check),
        ffmpeg_stack(args.check),
        transcriber,
    ]
    step("Result")
    if all(results):
        ok("Ready." if not args.check else "All components present.")
        sys.exit(0)
    bad("Some components are missing — see above.")
    sys.exit(1)


if __name__ == "__main__":
    main()
