#!/usr/bin/env bash
# End-to-end test: runs the full /watch pipeline against the deterministic
# test clip (tests/make_test_clip.sh) in an isolated cache dir and asserts
# every layer: frames, OCR, transcript, caching, focused windows, validation,
# and audio-only handling. Requires setup.py to have been run once.
set -euo pipefail
cd "$(dirname "$0")/.."

WATCH=skills/watch/scripts/watch.py
# Same order as common.bin_search_dirs(): WATCH_BIN_DIR -> 1.6.0 default ->
# 1.3.0–1.5.0 shared dir -> pre-1.3.0 in-repo bin -> PATH
FF=""
for d in "${WATCH_BIN_DIR:-}" "$HOME/.local/share/claude-video-mac/bin" \
         "$HOME/.cache/claude-video-mac/bin" ./skills/watch/bin; do
  [ -n "$d" ] && [ -x "$d/ffmpeg" ] && { FF="$d/ffmpeg"; break; }
done
[ -n "$FF" ] || FF=ffmpeg
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
SHEET=$(grep -o '[^ ]*sheets/sheet_[0-9]*\.jpg' <<<"$DIGEST" | head -1 || true)
if [ -n "$SHEET" ] && [ -f "$SHEET" ]; then pass "contact sheet listed and exists ($(basename "$SHEET"))"; else fail "no contact sheet in digest"; fi
if grep -qi "focused window" <<<"$DIGEST"; then fail "full run wrongly shows focused-window banner"; else pass "no focused-window banner on a full run"; fi

echo "== 2. cached re-run =="
DIGEST=$(python3 "$WATCH" "$CLIP" 2>"$ERR")
if grep -q "cache hit" "$ERR"; then pass "second run was a cache hit"; else fail "second run re-extracted"; fi

echo "== 2b. clamped --floor shares the default cache entry =="
DIGEST=$(python3 "$WATCH" "$CLIP" --floor 5 2>"$ERR")
if grep -q "cache hit" "$ERR"; then pass "--floor 5 (clamped to 2s) hit the default cache"; else fail "--floor 5 re-extracted despite clamping"; fi
DIGEST=$(python3 "$WATCH" "$CLIP" --no-repull --threshold 0.9 2>"$ERR")
if grep -q "cache hit" "$ERR"; then pass "--no-repull/--threshold (assembly-only) hit the extraction cache"; else fail "--no-repull re-extracted"; fi
DIGEST=$(python3 "$WATCH" "$CLIP" --locale en_us 2>"$ERR")
if grep -q "cache hit" "$ERR"; then pass "--locale en_us normalised to en-US and hit the same cache"; else fail "--locale en_us forked a new cache entry"; fi
DIGEST=$(python3 "$WATCH" "$CLIP" --summary-only 2>"$ERR")
if grep -q "cache hit" "$ERR" && ! grep -q "## Frames" <<<"$DIGEST" && ! grep -q '\.jpg' <<<"$DIGEST" && grep -qi "silicon" <<<"$DIGEST"; then
  pass "--summary-only on a cache hit: transcript kept, no frame/sheet paths"
else fail "--summary-only digest wrong"; fi
DIGEST=$(python3 "$WATCH" "$CLIP" 2>"$ERR")
if grep -q "^frames dir: " <<<"$DIGEST" && [ "$(grep -c '^t=.*\.jpg' <<<"$DIGEST")" -ge 1 ] && ! grep '^t=.*\.jpg' <<<"$DIGEST" | grep -q "$WATCH_CACHE_DIR"; then
  pass "frames dir printed once; frame lines are basenames"
else fail "frame lines still carry the full path"; fi

echo "== 2c. corrupt cache recovers instead of bricking =="
WD=$(ls -d "$WATCH_CACHE_DIR"/local_* | head -1)
echo "{ not json" > "$WD/done.json"
DIGEST=$(python3 "$WATCH" "$CLIP" 2>"$ERR") || { fail "corrupt done.json crashed the run"; cat "$ERR"; }
if grep -q "cache entry unreadable\|done (" "$ERR"; then pass "corrupt done.json treated as a miss"; else fail "corrupt done.json not handled"; fi
check "digest still intact after recovery" -F "SCENE ONE"

echo "== 2d. invalid numeric options rejected =="
if python3 "$WATCH" "$CLIP" --max-frames 0 >/dev/null 2>"$ERR"; then
  fail "accepted --max-frames 0"
else
  if grep -q "max-frames" "$ERR"; then pass "rejected --max-frames 0 with a friendly error"; else fail "rejection message unclear"; fi
fi
if python3 "$WATCH" "$CLIP" --scene 5 >/dev/null 2>"$ERR"; then
  fail "accepted --scene 5"
else
  pass "rejected --scene outside [0,1]"
fi

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

echo "== 8. purge + history =="
BEFORE=$(ls -d "$WATCH_CACHE_DIR"/local_* | wc -l | tr -d ' ')
python3 "$WATCH" "$CLIP" --purge >/dev/null 2>"$ERR"
AFTER=$(ls -d "$WATCH_CACHE_DIR"/local_* | wc -l | tr -d ' ')
if [ "$AFTER" -eq $((BEFORE - 1)) ] && grep -q "purged" "$ERR"; then pass "--purge removed exactly the clip's cache dir ($BEFORE -> $AFTER)"; else fail "--purge: $BEFORE -> $AFTER dirs"; fi
echo '{"https://example.invalid/v": "url_x"}' > "$WATCH_CACHE_DIR/url_ids.json"
python3 "$WATCH" --purge-history ignored >/dev/null 2>"$ERR"
if [ ! -f "$WATCH_CACHE_DIR/url_ids.json" ]; then pass "--purge-history removed url_ids.json"; else fail "--purge-history left url_ids.json"; fi

echo "== 9. doctor =="
if OUT=$(python3 "$WATCH" doctor 2>"$ERR"); then
  if grep -q "speech locales" <<<"$OUT" && grep -q "bin dir in use" <<<"$OUT" && grep -q "^OK" <<<"$OUT"; then pass "doctor exits 0 with a full report"; else fail "doctor report incomplete"; fi
else
  fail "doctor exited non-zero: $(tail -3 "$ERR")"
fi

echo "== 10. bad timestamps rejected =="
if python3 "$WATCH" "$CLIP" --start nan >/dev/null 2>"$ERR"; then fail "accepted --start nan"; else pass "rejected --start nan"; fi
if python3 "$WATCH" "$CLIP" --start 1:-30 >/dev/null 2>"$ERR"; then fail "accepted --start 1:-30"; else pass "rejected --start 1:-30"; fi

rm -f "$ERR"
echo
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
