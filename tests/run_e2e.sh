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
DIGEST=$(python3 "$WATCH" "$CLIP" --locale en 2>"$ERR")
if grep -q "cache hit" "$ERR" && grep -q -- "--locale en -> en-US" "$ERR"; then pass "bare --locale en resolved to en-US (logged) before the cache key"; else fail "bare --locale en not resolved: $(tail -1 "$ERR")"; fi
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

echo "== 7b. a folder of takes: one digest, side by side on top =="
"$FF" -y -hide_banner -loglevel error -i "$CLIP" -t 6 -c copy "$DIR/second.mp4"   # a shorter take
cp "$AUDIO" "$DIR/third audio.m4a"    # an audio-only take
printf 'junk' > "$DIR/._second.mp4"   # the AppleDouble file macOS leaves on non-Apple disks
DIGEST=$(python3 "$WATCH" "$DIR" 2>"$ERR") || { cat "$ERR"; exit 1; }
check "one folder digest" -F "# Folder: "
if [ "$(grep '^## Take' <<<"$DIGEST" | tr '\n' '|')" = "## Take 1 of 3: clip copy.mp4|## Take 2 of 3: second.mp4|## Take 3 of 3: third audio.m4a|" ]; then pass "three takes in name order, the AppleDouble file skipped"; else fail "take list wrong: $(grep '^## Take' <<<"$DIGEST" | tr '\n' ' ')"; fi
if grep -q "cache hit" "$ERR"; then pass "the take watched before is a cache hit"; else fail "the cached take re-extracted"; fi
SBS=$(grep -o '[^ ]*compare/[0-9a-f]*/compare_[0-9]*\.jpg' <<<"$DIGEST" | head -1 || true)
if [ -n "$SBS" ] && [ -f "$SBS" ]; then pass "side-by-side sheet listed and exists"; else fail "no side-by-side sheet in the digest"; fi
if awk '/^## Side by side/ { s = NR } /^## Take 1 of/ { t = NR } END { exit !(s && t && s < t) }' <<<"$DIGEST"; then pass "the side-by-side sheet comes before the takes"; else fail "side by side is not on top"; fi
if [ "$(grep -c '^### Transcript (timestamped)' <<<"$DIGEST")" -eq 3 ] && [ "$(grep -cF 'SCENE ONE' <<<"$DIGEST")" -ge 2 ] && [ "$(grep -c '^# ' <<<"$DIGEST")" -eq 1 ]; then pass "each take keeps its own digest, one level down"; else fail "a take digest is missing or not nested"; fi
TAKE_DIRS=$(sed -n 's/^frames dir: \(.*\)\/frames$/\1/p' <<<"$DIGEST")
AGAIN=$(python3 "$WATCH" "$DIR" 2>"$ERR")
if grep -q "cache hit" "$ERR" && [ "$(grep -o '[^ ]*compare/[0-9a-f]*/compare_[0-9]*\.jpg' <<<"$AGAIN" | head -1)" = "$SBS" ]; then pass "a re-run reuses the same side-by-side sheet"; else fail "re-run drew a different side-by-side sheet"; fi
DIGEST=$(python3 "$WATCH" "$DIR" --start 1 --end 4 2>"$ERR") || { cat "$ERR"; exit 1; }
SBS=$(grep -o '[^ ]*compare/[0-9a-f]*/compare_[0-9]*\.jpg' <<<"$DIGEST" | head -1 || true)
if grep -qF "(the same window in every take)" <<<"$DIGEST" && [[ "$SBS" == */windows/* ]] && [ -f "$SBS" ]; then pass "a window run has its own side-by-side sheet"; else fail "folder window run: ${SBS:-no sheet}"; fi
INWIN=$(awk '/^t=[0-9]+:[0-9]+\.[0-9]+  frame_/ { split(substr($1, 3), p, ":"); s = p[1] * 60 + p[2]; n++; if (s >= 0.9 && s <= 4.1) k++ } END { print (n && n == k) ? n : 0 }' <<<"$DIGEST")
if [ "$INWIN" -ge 20 ] && [ "$(grep -c 'focused window: 00:01–00:04.* only this range' <<<"$DIGEST")" -eq 3 ]; then pass "every take is windowed: $INWIN frames, all inside 1-4 s, the audio take included"; else fail "window not applied to every take (${INWIN} frames in window)"; fi
DIGEST=$(python3 "$WATCH" "$DIR" --summary-only 2>"$ERR")
if ! grep -q "## Side by side" <<<"$DIGEST" && ! grep -q '\.jpg' <<<"$DIGEST" && grep -qF "## Take 3 of 3" <<<"$DIGEST"; then pass "--summary-only: the takes, and no image paths at all"; else fail "--summary-only folder digest wrong"; fi
python3 "$WATCH" "$DIR" --purge >/dev/null 2>"$ERR"
LEFT=0; for d in $TAKE_DIRS; do [ -e "$d" ] && LEFT=$((LEFT+1)); done
if [ -n "$TAKE_DIRS" ] && [ "$LEFT" -eq 0 ] && [ "$(grep -c 'purged cache for' "$ERR")" -eq 3 ] && ! ls -d "$WATCH_CACHE_DIR"/local_set_* >/dev/null 2>&1; then pass "--purge on the folder removes every take's cache and its sheets"; else fail "folder purge left $LEFT take dir(s): $(tr '\n' ' ' < "$ERR")"; fi

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

echo "== 11. cache cleanup =="
OLD="$WATCH_CACHE_DIR/local_e2eunused000000"
mkdir -p "$OLD" && echo x > "$OLD/watch.md"
MONTH_AGO=$(date -v-30d +%Y%m%d%H%M)
touch -t "$MONTH_AGO" "$OLD/watch.md"
WATCH_CACHE_MAX_AGE_DAYS=0 python3 "$WATCH" "$AUDIO" >/dev/null 2>"$ERR"
if [ -d "$OLD" ]; then pass "WATCH_CACHE_MAX_AGE_DAYS=0 keeps a month-old video"; else fail "cleanup ran with WATCH_CACHE_MAX_AGE_DAYS=0"; fi
AWD="$WATCH_CACHE_DIR/$(sed -n 's/.*cache hit (\(local_[0-9a-f]*\)).*/\1/p' "$ERR" | head -1)"
find "$AWD" -exec touch -t "$MONTH_AGO" {} +   # the video being watched is a month old too
python3 "$WATCH" "$AUDIO" >/dev/null 2>"$ERR"
if [ ! -d "$OLD" ] && grep -q "cache cleanup: removed 1 video" "$ERR"; then pass "a run deletes a video unused for 30 days"; else fail "month-old video survived a run: $(grep -i cleanup "$ERR")"; fi
if [ -d "$AWD" ] && grep -q "cache hit" "$ERR"; then pass "a month-old video watched again is still a cache hit"; else fail "cleanup took the video being watched"; fi

echo "== 12. short clips are sampled densely, long ones as before =="
gap_of() { sed -n 's/.*largest gap between frames \([0-9.]*\)s.*/\1/p' <<<"$1" | head -1; }
DIGEST=$(python3 "$WATCH" "$CLIP" --no-cache 2>"$ERR")
NFRAMES=$(grep -c '^t=.*\.jpg' <<<"$DIGEST" || true)
GAP=$(gap_of "$DIGEST")
if grep -q "short span: every 0.25s, no dedup" "$ERR" && ! grep -q "perceptual dedup" "$ERR"; then pass "the 12 s clip is a short span (0.25 s floor, no dedup)"; else fail "12 s clip not sampled as a short span: $(grep extracting "$ERR")"; fi
FINE=$(awk '/^t=[0-9]+:[0-9]+\.[0-9][0-9]  frame_/ { l = substr($1, 3); gsub(":", "m", l); if (index($2, "_t" l "s.jpg")) n++ } END { print n + 0 }' <<<"$DIGEST")
if [ "$FINE" -eq "$NFRAMES" ]; then pass "every short-span frame is labelled and named to the hundredth"; else fail "$FINE of $NFRAMES frames carry a matching hundredth label: $(grep -m2 '^t=.*\.jpg' <<<"$DIGEST" | tr '\n' ' ')"; fi
if [ "$NFRAMES" -ge 40 ] && awk -v g="$GAP" 'BEGIN{exit !(g != "" && g <= 0.3)}'; then pass "12 s clip: $NFRAMES frames, largest gap ${GAP}s"; else fail "12 s clip: $NFRAMES frames, largest gap ${GAP:-?}s"; fi
LONG="$WATCH_CACHE_DIR/long_clip.mp4"
"$FF" -y -hide_banner -loglevel error -stream_loop 1 -i "$CLIP" -c copy "$LONG"
DIGEST=$(python3 "$WATCH" "$LONG" 2>"$ERR")
WHOLE=$(grep -cE '^t=[0-9]+:[0-9]{2}  frame_[0-9]{4}_t[0-9]+m[0-9]{2}s\.jpg$' <<<"$DIGEST" || true)
if grep -Eq "floor=2(\.0)?s" "$ERR" && grep -q "perceptual dedup: [0-9]" "$ERR" && ! grep -q "short span" "$ERR"; then pass "a 24 s clip keeps the 2 s floor and dedup"; else fail "24 s clip sampling changed: $(grep -E 'extracting|dedup' "$ERR")"; fi
if [ "$WHOLE" -ge 1 ] && [ "$WHOLE" -eq "$(grep -c '^t=.*\.jpg' <<<"$DIGEST" || true)" ]; then pass "its $WHOLE frames keep whole-second labels"; else fail "24 s clip labels: $(grep -m2 '^t=.*\.jpg' <<<"$DIGEST" | tr '\n' ' ')"; fi
DIGEST=$(python3 "$WATCH" "$LONG" --start 0 --end 6 2>"$ERR")
if grep -q "short span: every 0.25s" "$ERR" && [ "$(grep -c '^t=.*\.jpg' <<<"$DIGEST" || true)" -ge 20 ]; then pass "a 6 s focused window on it is a short span"; else fail "6 s window not dense: $(grep extracting "$ERR")"; fi

python3 "$WATCH" "$CLIP" --start 2 --end 90 >/dev/null 2>"$ERR"
if grep -q "short span: every 0.25s" "$ERR"; then pass "an --end past the 12 s clip is still a short span"; else fail "--end past the clip went sparse: $(grep extracting "$ERR")"; fi

rm -f "$ERR"
echo
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
