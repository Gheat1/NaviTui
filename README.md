<div align="center">

# ♪ NaviTui

**A fast, animated terminal player for [Navidrome](https://www.navidrome.org/).**

Cover art rendered right in your terminal, playback through mpv, five themes
via [ricekit](https://github.com/Gheat1/ricekit) — and everything moves.

<img src="assets/shot-main.png" alt="NaviTui" width="100%">

</div>

---

## what it does

- **songs first** — no album grids to dig through: one sidebar with all
  tracks / recently added / recently played / most played / starred /
  **shuffle everything**, your playlists right under it, and one big track
  list. Albums and artists still exist — inside search (`/`), where they
  belong
- **playlists you can actually edit** — create one from the sidebar, add
  any track with `p`
- **search that queues** — `enter` plays, `a` queues, `A` slots it right
  after the current song
- **real cover art** — kitty graphics protocol or sixel where available,
  truecolor half-cells everywhere else (`NAVITUI_ART=auto|tgp|sixel|halfcell|unicode|off`)
- **a queue that behaves** — shows what's *up next* (played tracks dim out
  above; scroll up for history), add (`a`), play-next (`A`), remove, clear,
  shuffle that keeps the current track, repeat off/all/one; the queue —
  including your position *inside the current song* — survives a restart
- **alive by default** — the wordmark shimmers, the visualizer pulses with
  playback, the progress bar has 1/8-cell resolution and breathes, long
  titles marquee, panels fade in; all driven by one 8fps heartbeat that
  repaints a handful of cells
- **cache-first** — every pane renders instantly from disk, then refreshes
  silently in the background (auto-refresh every 3 minutes)
- **scrobbles & stars** — now-playing + submission scrobbles at 50%, star and
  unstar songs/albums/artists with `f`
- **full mouse support** — click anything, drag the panel dividers, click the
  progress bar to seek, click the volume gauge, click shuffle/repeat
- **five themes**, live-previewed (`t` cycles, `T` picks) — including `clear`
  (your terminal's transparency shows through) and `system` (your terminal's
  own ANSI palette)

<div align="center">
<img src="assets/shot-search.png" alt="search" width="49%">
<img src="assets/shot-void.png" alt="void theme" width="49%">
</div>

## install

You need **libmpv** for playback (everything else ships with the package):

```sh
# arch
sudo pacman -S mpv
# debian/ubuntu
sudo apt install libmpv2
# macos
brew install mpv
# windows: put libmpv-2.dll on PATH — https://mpv.io/installation/
```

then

```sh
uv tool install git+https://github.com/Gheat1/NaviTui
navitui
```

First run asks for your server, username and password; the password is never
stored — only the salted token (chmod 600). Works with Navidrome and any
Subsonic-compatible server. Try it against the public demo:
`https://demo.navidrome.org` / `demo` / `demo`.

## keys

`?` shows everything. The ones you'll use constantly:

| | |
| --- | --- |
| `space` | play / pause |
| `enter` / double-click | play (track, view, playlist) |
| `n` / `b` | next / previous |
| `←` `→` | seek (`shift` for 30s) |
| `a` / `A` | queue / play next (works in search too) |
| `p` | add track to a playlist |
| `s` / `r` | shuffle / repeat |
| `f` | star / unstar |
| `/` | search |
| `h` `l` `j` `k` | move around, vim-style |
| `t` / `T` | themes |

## the suite

- [**ricekit**](https://github.com/Gheat1/ricekit) — the design system this is built on
- [**ltui**](https://github.com/Gheat1/ltui) — a fast, beautiful TUI for Linear

## license

[MIT](LICENSE) — made by [@Gheat1](https://github.com/Gheat1)
