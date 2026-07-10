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
- **README screenshots come from the tools, never a real library.**
  `tools/shots.sh` captures the real app in kitty on an empty Hyprland
  workspace (true pixel cover art) via `tools/demo.py`'s posed states;
  `tools/screenshots.py` is the headless SVG fallback. Both use the mocked
  client and generated art — zero network, zero real data.
- **mpv callbacks arrive on mpv's thread.** Anything touching the UI must be
  scheduled with `loop.call_soon_threadsafe` — never a blocking call (see
  `_mpv_position`/`_mpv_track_end` and the sharp-edges table below).
- **Every kit theme must keep working** — including `system` (ANSI): color
  blending degrades to flat styles via `anim.blend`/`can_blend`; never bake
  palette values in at import time.

## file map

```
navitui/app.py        the app: sidebar+tracks layout, workers, playback glue, actions
navitui/api.py        async Subsonic client (httpx), token auth, art cache
navitui/player.py     libmpv wrapper + NullPlayer fallback when mpv is absent
navitui/playqueue.py  queue/shuffle/repeat logic (no UI in here)
navitui/anim.py       animation primitives: shimmer, smooth_bar, marquee, viz
navitui/widgets.py    Logo, Visualizer, NowPlaying (the animated transport)
navitui/art.py        CoverArt widget, protocol picking, NAVITUI_ART override
navitui/screens.py    onboarding, search modal, InputModal, LyricsModal
navitui/config.py     player.toml: keybinds, replaygain, gapless, integrations
navitui/nowplaying.py OS media façade: picks a backend by platform, wraps controls thread-safe
navitui/mpris.py      linux backend — MPRIS2 via dbus-fast (asyncio-native, two-way controls)
navitui/macos_media.py   macos backend — MPNowPlayingInfoCenter/MPRemoteCommandCenter (pyobjc)
navitui/windows_media.py windows backend — System Media Transport Controls (winrt)
navitui/integrations.py  desktop notifications + optional discord presence
navitui/models.py     dataclasses that round-trip through the JSON cache
tools/screenshots.py  headless SVG screenshot generator + FakeClient
tools/demo.py         poses the real app in a real terminal (states: main/playlist/search/void)
tools/shots.sh        captures those states with grim → assets/shot-*.png
```

## sharp edges (beyond ricekit's DESIGN.md table)

| gotcha | rule |
| --- | --- |
| `Widget.visual_style` (textual 8) | caches the blended text background while an ancestor's opacity is still animating — `pop_in` on a background-bearing box leaves smudged text backgrounds. `screens.settle_pop_in` busts the cache after the fade. |
| mpv callbacks | arrive on mpv's thread and must never block: `call_from_thread` deadlocks against `terminate()` on quit (UI joins the event thread while the event thread waits for the UI). Use `loop.call_soon_threadsafe`, throttle `time-pos` to ~0.25s steps, silence observers with `_closing` before `terminate()` |
| textual action args | are Python literals: `enqueue(True)`, never `enqueue(true)` |
| two `run_test` sessions in one process | can wedge with a constantly-animating app — `tools/screenshots.py` isolates each phase in a subprocess |
| OS media backends (mac/win) | metadata *display* is solid; receiving media-key presses into a terminal process needs a native run-loop/message-pump thread and is UNVERIFIED on Linux dev boxes — must be tested on a real Mac/Windows machine. All backends are import-safe and no-op off their platform; `nowplaying.create_nowplaying()` dispatches, and `_thread_safe` marshals their callbacks onto the app loop. |

## testing

Headless, against the mocked client from `tools/screenshots.py`, or live
against the public demo server (`https://demo.navidrome.org`, demo/demo —
read-only tests only). Isolate state with `HOME=$(mktemp -d)` and pass
`ao="null"` to `NaviTuiApp` so mpv needs no audio device. Screenshot SVGs
via `app.save_screenshot()` for visual review.
