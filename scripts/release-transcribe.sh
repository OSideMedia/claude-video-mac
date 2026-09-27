#!/usr/bin/env bash
# Build the SpeechTranscriber CLI as a release asset.
#
#   bash scripts/release-transcribe.sh [out-dir]
#
# Compiles skills/watch/scripts/transcribe-swift/main.swift with swiftc -O,
# ad-hoc codesigns the result, prints its SHA-256, and prints — WITHOUT
# running — the `gh release upload` command that publishes it. Users then
# skip the Xcode prerequisite with:
#
#   python3 skills/watch/scripts/setup.py \
#     --transcribe-url https://github.com/OSideMedia/claude-video-mac/releases/download/vX.Y.Z/transcribe \
#     --transcribe-sha256 <the hash printed below>
#
# Nothing here touches the installed binary or any cache dir.
set -euo pipefail
cd "$(dirname "$0")/.."

SRC=skills/watch/scripts/transcribe-swift/main.swift
OUT_DIR="${1:-dist}"
OUT="$OUT_DIR/transcribe"
VERSION=$(python3 -c 'import json; print(json.load(open(".claude-plugin/plugin.json"))["version"])')

[ "$(uname -s)" = "Darwin" ] || { echo "release-transcribe.sh: macOS only (needs the Speech framework)" >&2; exit 1; }
[ "$(uname -m)" = "arm64" ] || { echo "release-transcribe.sh: build on Apple Silicon so the asset is arm64" >&2; exit 1; }
command -v swiftc >/dev/null || { echo "release-transcribe.sh: swiftc not found (install Xcode or Command Line Tools)" >&2; exit 1; }

mkdir -p "$OUT_DIR"
echo "== building $SRC -> $OUT (swiftc -O, Swift 6 language mode) =="
swiftc -O -swift-version 6 "$SRC" -o "$OUT"
chmod 755 "$OUT"
codesign --force --sign - "$OUT"
codesign --verify --verbose=1 "$OUT" 2>&1 | sed 's/^/  /'

echo
echo "== asset =="
file -b "$OUT" | sed 's/^/  /'
SHA=$(shasum -a 256 "$OUT" | awk '{print $1}')
echo "  size:   $(stat -f %z "$OUT") bytes"
echo "  sha256: $SHA"
echo "  source: $(shasum -a 256 "$SRC" | awk '{print $1}')  ($SRC)"

echo
echo "== to publish (NOT run by this script) =="
echo "  gh release upload v$VERSION $OUT --clobber"
echo
echo "== users then install without Xcode =="
echo "  python3 skills/watch/scripts/setup.py \\"
echo "    --transcribe-url https://github.com/OSideMedia/claude-video-mac/releases/download/v$VERSION/transcribe \\"
echo "    --transcribe-sha256 $SHA"
