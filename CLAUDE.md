# CLAUDE.md

Guidance for AI agents (and curious humans) working in this repo.

## what this is

`navitui` is a Python TUI music player for Navidrome/Subsonic servers, built
on [Textual](https://github.com/Textualize/textual) and
[ricekit](https://github.com/Gheat1/ricekit) (the design system — read its
`DESIGN.md` before changing any UI). Playback is libmpv via `python-mpv`;
cover art is `textual-image` (kitty/sixel/halfcell/unicode).

Design priorities, in order:

1. **fast** — cache-first everywhere; the UI never blocks on the network
2. **alive** — one 8fps heartbeat drives every animation; each tick repaints
   only a few cells; never add a timer per widget
3. **pretty** — ricekit themes/palette at render time, nerd-font icons as
   `\uXXXX` escapes only (raw PUA glyphs do not survive patch tooling)

## hard rules

- **No AI attribution in commits.** No `Co-Authored-By`, no "generated with"
  trailers. Commit as the repo owner.
- **README screenshots come from `tools/screenshots.py`** (mocked client,
  generated art, zero network) — never from a real library.
- **mpv callbacks arrive on mpv's thread.** Anything touching the UI must be
  scheduled with `loop.call_soon_threadsafe` — never a blocking call (see
  `_mpv_position`/`_mpv_track_end` and the sharp-edges table below).
- **Every kit theme must keep working** — including `system` (ANSI): color
  blending degrades to flat styles via `anim.blend`/`can_blend`; never bake
  palette values in at import time.

## file map

```
navitui/app.py        the app: layout, tabs, workers, playback glue, actions
navitui/api.py        async Subsonic client (httpx), token auth, art cache
navitui/player.py     libmpv wrapper + NullPlayer fallback when mpv is absent
navitui/playqueue.py  queue/shuffle/repeat logic (no UI in here)
navitui/anim.py       animation primitives: shimmer, smooth_bar, marquee, viz
navitui/widgets.py    Logo, Visualizer, NowPlaying (the animated transport)
navitui/art.py        CoverArt widget, protocol picking, NAVITUI_ART override
navitui/screens.py    onboarding (validates live, stores chmod 600) + search
navitui/models.py     dataclasses that round-trip through the JSON cache
tools/screenshots.py  README asset generator (fake data, real playback)
```

## sharp edges (beyond ricekit's DESIGN.md table)

| gotcha | rule |
| --- | --- |
| `Widget.visual_style` (textual 8) | caches the blended text background while an ancestor's opacity is still animating — `pop_in` on a background-bearing box leaves smudged text backgrounds. `screens.settle_pop_in` busts the cache after the fade. |
| mpv callbacks | arrive on mpv's thread and must never block: `call_from_thread` deadlocks against `terminate()` on quit (UI joins the event thread while the event thread waits for the UI). Use `loop.call_soon_threadsafe`, throttle `time-pos` to ~0.25s steps, silence observers with `_closing` before `terminate()` |
| textual action args | are Python literals: `enqueue(True)`, never `enqueue(true)` |
| two `run_test` sessions in one process | can wedge with a constantly-animating app — `tools/screenshots.py` isolates each phase in a subprocess |

## testing

Headless, against the mocked client from `tools/screenshots.py`, or live
against the public demo server (`https://demo.navidrome.org`, demo/demo —
read-only tests only). Isolate state with `HOME=$(mktemp -d)` and pass
`ao="null"` to `NaviTuiApp` so mpv needs no audio device. Screenshot SVGs
via `app.save_screenshot()` for visual review.
