#!/usr/bin/env bash
# Real-terminal screenshots: sit on an EMPTY workspace and run this — each
# demo state opens a kitty window that tiles fullscreen, grim captures it
# (true kitty-graphics cover art included), then it closes itself.
# Usage: tools/shots.sh [outdir]
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$REPO/assets}"
TITLE="navitui-demo"
mkdir -p "$OUT"

shot() {
    local state="$1" fontsize="$2"
    kitty --title "$TITLE" \
        -o "font_size=$fontsize" \
        -o window_padding_width=18 \
        -o confirm_os_window_close=0 \
        -e "$REPO/.venv/bin/python" "$REPO/tools/demo.py" "$state" &
    local pid=$!
    sleep 11  # demo drive settles ~7s in

    local geom
    geom=$(hyprctl clients -j | python3 -c "
import json, sys
for c in json.load(sys.stdin):
    if c.get('initialTitle') == '$TITLE':
        print(f\"{c['at'][0]},{c['at'][1]} {c['size'][0]}x{c['size'][1]}\")
        break
")
    if [ -z "$geom" ]; then
        echo "!! no window for state=$state" >&2
    else
        grim -g "$geom" "$OUT/shot-$state.png"
        echo "$OUT/shot-$state.png  ($geom)"
    fi
    kill "$pid" 2>/dev/null || true
    sleep 1
}

shot main   16
shot playlist 16
shot search 16
shot void   16
