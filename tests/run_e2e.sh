#!/usr/bin/env bash
# End-to-end test: runs the full /watch pipeline against the deterministic
# test clip (tests/make_test_clip.sh) in an isolated cache dir and asserts
# every layer: frames, OCR, transcript, caching, focused windows, validation,
# and audio-only handling. Requires setup.py to have been run once.
set -euo pipefail
cd "$(dirname "$0")/.."

WATCH=skills/watch/scripts/watch.py
FF=./skills/watch/bin/ffmpeg
CLIP=tests/assets/test_clip.mp4

PASS=0; FAIL=0
pass() { echo "  ✓ $1"; PASS=$((PASS+1)); }
fail() { echo "  ✗ $1"; FAIL=$((FAIL+1)); }
check() { # check <description> <grep-args...> -- input from $DIGEST
  local desc=$1; shift
  if grep -q "$@" <<<"$DIGEST"; then pass "$desc"; else fail "$desc"; fi
}

[ -f "$CLIP" ] || bash tests/make_test_clip.sh

export WATCH_CACHE_DIR="$(mktemp -d)"
trap 'rm -rf "$WATCH_CACHE_DIR"' EXIT
ERR="$WATCH_CACHE_DIR/err.log"
echo "cache: $WATCH_CACHE_DIR"

echo "== 1. full run =="
DIGEST=$(python3 "$WATCH" "$CLIP" 2>"$ERR") || { cat "$ERR"; exit 1; }
for s in "SCENE ONE" "SCENE TWO" "SCENE THREE" "SCENE FOUR"; do
  check "OCR found '$s'" -F "$s"
done
check "transcript heard 'silicon'"     -i "silicon"
check "transcript heard 'transcriber'" -i "transcriber"
NFRAMES=$(grep -c '^t=.*\.jpg' <<<"$DIGEST" || true)
if [ "$NFRAMES" -ge 4 ]; then pass "listed $NFRAMES frames (>= 4)"; else fail "only $NFRAMES frames listed"; fi
if grep -qi "focused window" <<<"$DIGEST"; then fail "full run wrongly shows focused-window banner"; else pass "no focused-window banner on a full run"; fi

echo "== 2. cached re-run =="
DIGEST=$(python3 "$WATCH" "$CLIP" 2>"$ERR")
if grep -q "cache hit" "$ERR"; then pass "second run was a cache hit"; else fail "second run re-extracted"; fi

echo "== 3. focused window (3s-6s) =="
DIGEST=$(python3 "$WATCH" "$CLIP" --start 3 --end 6 2>"$ERR") || { cat "$ERR"; exit 1; }
check "digest shows focused-window banner" -i "focused window"
BAD_TS=$(grep '^t=.*\.jpg' <<<"$DIGEST" | grep -cv '^t=00:0[3-6]' || true)
if [ "$BAD_TS" -eq 0 ]; then pass "all focused frames within 00:03-00:06"; else fail "$BAD_TS frame(s) outside the window"; fi

echo "== 4. full-video cache survives the focused run =="
DIGEST=$(python3 "$WATCH" "$CLIP" 2>"$ERR")
if grep -q "cache hit" "$ERR"; then pass "full-video run still cached"; else fail "focused run clobbered the full-video cache"; fi

echo "== 5. invalid window rejected =="
if python3 "$WATCH" "$CLIP" --start 6 --end 3 >/dev/null 2>"$ERR"; then
  fail "accepted --start 6 --end 3"
else
  if grep -qi "window" "$ERR"; then pass "rejected with a friendly window error"; else fail "rejected but message unclear: $(tail -1 "$ERR")"; fi
fi

echo "== 6. audio-only source =="
AUDIO="$WATCH_CACHE_DIR/test_audio.m4a"
"$FF" -y -hide_banner -loglevel error -i "$CLIP" -vn -c:a copy "$AUDIO"
DIGEST=$(python3 "$WATCH" "$AUDIO" 2>"$ERR") || { cat "$ERR"; exit 1; }
check "digest flags audio-only"        -i "audio-only"
check "audio-only transcript present"  -i "silicon"

echo "== 7. local paths: folder + tilde-style resolution =="
DIR="$WATCH_CACHE_DIR/clipdir"
mkdir -p "$DIR"
cp "$CLIP" "$DIR/clip copy.mp4"   # space in the name on purpose
DIGEST=$(python3 "$WATCH" "$DIR" 2>"$ERR") || { cat "$ERR"; exit 1; }
check "folder input resolved to the video inside" -F "SCENE ONE"
# same folder given via a relative path must hit the same cache entry
DIGEST=$(cd "$WATCH_CACHE_DIR" && python3 "$OLDPWD/$WATCH" "clipdir" 2>"$ERR")
if grep -q "cache hit" "$ERR"; then pass "relative path hit the same cache"; else fail "relative path re-extracted"; fi
# a folder with two media files must be rejected with the file list
cp "$CLIP" "$DIR/second.mp4"
if python3 "$WATCH" "$DIR" >/dev/null 2>"$ERR"; then
  fail "accepted an ambiguous folder"
else
  if grep -q "specify one" "$ERR"; then pass "ambiguous folder rejected with file list"; else fail "ambiguous folder error unclear"; fi
fi

rm -f "$ERR"
echo
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
