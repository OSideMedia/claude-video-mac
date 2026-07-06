#!/usr/bin/env bash
# Generates a controllable test clip that exercises every gate:
#   - 4 hard scene cuts (scene-detection)
#   - known on-screen text per scene (OCR gate)
#   - real spoken audio via macOS `say` (SpeechTranscriber gate)
# Output: tests/assets/test_clip.mp4
set -euo pipefail
cd "$(dirname "$0")/.."
# shared bin (survives plugin updates) -> legacy in-repo bin -> PATH
FF="${WATCH_BIN_DIR:-$HOME/.cache/claude-video-mac/bin}/ffmpeg"
[ -x "$FF" ] || FF=./skills/watch/bin/ffmpeg
[ -x "$FF" ] || FF=ffmpeg
FONT=/System/Library/Fonts/Supplemental/Arial.ttf
OUT=tests/assets
mkdir -p "$OUT"

# 1) Speech track (distinctive words, no homophones).
say -o "$OUT/speech.aiff" \
  "Scene one. Apple Silicon. Scene two. Video Toolbox decode. Scene three. Vision text recognition. Scene four. On device speech transcriber test."

# 2) Four 3-second scenes, each a solid color with centered known text.
declare -a COLORS=(navy darkgreen maroon black)
declare -a LINE1=("SCENE ONE" "SCENE TWO" "SCENE THREE" "SCENE FOUR")
declare -a LINE2=("APPLE SILICON" "VIDEO TOOLBOX" "VISION OCR" "SPEECH TRANSCRIBER")
: > "$OUT/concat.txt"
for i in 0 1 2 3; do
  $FF -y -hide_banner -loglevel error \
    -f lavfi -i "color=c=${COLORS[$i]}:s=640x360:r=30:d=3" \
    -vf "drawtext=fontfile=$FONT:text='${LINE1[$i]}':fontcolor=white:fontsize=48:x=(w-tw)/2:y=120,\
drawtext=fontfile=$FONT:text='${LINE2[$i]}':fontcolor=white:fontsize=36:x=(w-tw)/2:y=200" \
    -pix_fmt yuv420p "$OUT/seg_$i.mp4"
  echo "file 'seg_$i.mp4'" >> "$OUT/concat.txt"
done

# 3) Concat scenes, mux with speech (audio resampled to AAC).
$FF -y -hide_banner -loglevel error -f concat -safe 0 -i "$OUT/concat.txt" \
  -i "$OUT/speech.aiff" \
  -c:v libx264 -pix_fmt yuv420p -c:a aac -b:a 128k \
  -map 0:v:0 -map 1:a:0 "$OUT/test_clip.mp4"

# cleanup intermediates
rm -f "$OUT"/seg_*.mp4 "$OUT/concat.txt" "$OUT/speech.aiff"
echo "wrote $OUT/test_clip.mp4"
