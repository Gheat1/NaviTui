<div align="center">

# ♪ NaviTui

**A fast, animated terminal player for [Navidrome](https://www.navidrome.org/).**

Cover art rendered right in your terminal, playback through mpv, five themes
via [ricekit](https://github.com/Gheat1/ricekit) — offline downloads, karaoke
lyrics, endless radio, and a control API. Everything moves.

<img src="assets/shot-main.png" alt="NaviTui" width="100%">

</div>

---

## what it does

**songs-first library**
- one sidebar — all tracks / recently added / recently played / most played /
  starred / **shuffle everything**, your playlists right under it, plus
  **genres** (`y`), **podcasts** and **internet radio** — and one big track list
- albums and artists live inside search (`/`), where they belong
- **type-to-filter** (`\`) narrows the current list instantly as you type, no
  network round-trip
- **multi-select** (`v`) — tag rows, then queue / playlist-add / star /
  download the whole selection at once

**playback & the queue**
- a queue that shows what's *up next* (played tracks dim above; scroll up for
  history): add (`a`), play-next (`A`), remove, clear, reorder (`ctrl+↑/↓`)
- **shuffle** that reshuffles the up-next and starts a fresh view on a random
  track; **repeat** off / all / one
- **sleep timer** (`<`) and **playback speed** (`>`, great for podcasts &
  audiobooks); **crossfade** + next-track prefetch for seamless transitions
- the queue — including your position *inside the current song* — survives a
  restart

**offline**
- **download** a track (`d`), a whole view/playlist (`D`), or your entire
  library (`ctrl+d`) — pinned tracks get a ✓ and play locally forever
- **offline mode** (`O`) plays only what's downloaded and skips the network;
  stars / ratings / scrobbles you make offline are **queued and flushed** when
  you reconnect
- pick a streaming **bitrate cap** (`Q`) for tight connections — downloads
  always keep the original file

**karaoke lyrics**
- timed, scrolling, highlighted lyrics (`L`) that ride the beat, and the same
  view in **zen mode** (`z`) — a big centred cover + lyrics splash

<div align="center">
<img src="assets/shot-lyrics.svg" alt="synced lyrics" width="70%">
</div>

**discovery**
- **endless radio** — start a station from any track or artist (`i`), or let
  it **autoplay similar tracks forever** when the queue drains (`I`)
- **bookmarks** (`w` / `W`) to save and jump back to a spot in long tracks
- **listening stats** (`ctrl+w`) — a local mini-wrapped: top tracks & artists,
  this-week counts, an activity sparkline, streaks

**part of your desktop**
- media keys & waybar/playerctl via **MPRIS2**
- **desktop notifications** on track change (cover as the icon) with
  **prev / play-pause / next action buttons**
- optional **Discord rich presence** with a live progress bar
- optional **ListenBrainz** scrobbling alongside your server's

**control & automation**
- a local **control API** (unix socket) so other tools can drive a running
  player — used by:
- **`navitui-remote`** — a scriptable CLI (`navitui-remote status`, `next`,
  `play "<query>"`, …) perfect for global hotkeys
- **`navitui-mcp`** — an MCP server so Claude and other agents can control
  playback, search, download, and read what's now playing
- **jukebox mode** (`J`) — drive the *server's own* audio output, for a
  headless box wired to speakers

**alive & pretty**
- the wordmark shimmers, the **visualizer reacts to real playback loudness**,
  the progress bar has 1/8-cell resolution and breathes, long titles marquee,
  panels fade in — all from one 8fps heartbeat that repaints a handful of cells
- **album-art theming** tints the UI toward the current cover's colors
- **real cover art** — kitty graphics or sixel where available, truecolor
  half-cells everywhere else
  (`NAVITUI_ART=auto|tgp|sixel|halfcell|unicode|off`)
- **cache-first** — every pane renders instantly from disk, then refreshes
  silently (auto every 3 minutes)
- **five themes**, live-previewed (`t` cycles, `T` picks) — including `clear`
  (terminal transparency shows through) and `system` (your terminal's ANSI
  palette)

**more**
- full **playlist editing** — create, add (`p`), remove (`P`), reorder
  (`shift+↑/↓`), rename (`ctrl+r`), delete (`ctrl+x`), or **save the queue as a
  playlist** (`ctrl+s`)
- **scrobbles, stars & ratings** — star (`f`), rate 1-5 on the number row
- **command palette** (`ctrl+p`) — every action, fuzzy-searchable
- **export** the now-playing card as an SVG (`C`), copy a **share link** (`S`)
- **vim repeat counts** (`3j` moves down three), full **mouse support** (click
  the progress bar to seek, drag the dividers, click shuffle/repeat)
- **yours to tune** — `~/.config/navitui/player.toml` (written with comments on
  first run): remap every key, ReplayGain, gapless, crossfade, bitrate, jukebox,
  Discord & ListenBrainz

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
uv tool install "navitui[integrations] @ git+https://github.com/Gheat1/NaviTui"
navitui
```

`[integrations]` pulls the optional MPRIS / Discord / ListenBrainz / notification
bits — drop it for the bare player. Add the `mcp` extra
(`navitui[integrations,mcp]`) if you want the `navitui-mcp` agent server.

First run asks for your server, username and password; the password is never
stored — only the salted token (chmod 600). Works with Navidrome and any
Subsonic-compatible server. Try it against the public demo:
`https://demo.navidrome.org` / `demo` / `demo`.

## keys

`?` shows everything; `ctrl+p` searches every action. The ones you'll use
constantly:

| | | | |
| --- | --- | --- | --- |
| `space` | play / pause | `d` `D` | download track / view |
| `enter` | play track / view / playlist | `ctrl+d` | download library |
| `n` / `b` | next / previous | `O` | offline mode |
| `←` `→` | seek (`shift` = 30s) | `\` | filter the list |
| `a` / `A` | queue / play next | `v` | multi-select |
| `s` / `r` | shuffle / repeat | `L` | lyrics |
| `i` / `I` | start radio / autoplay | `z` | zen splash |
| `f` | star / unstar | `<` `>` | sleep timer / speed |
| `p` | add to playlist | `y` | browse by genre |
| `/` | search | `ctrl+w` | listening stats |
| `h` `l` `j` `k` | move around, vim-style | `t` / `T` | themes |

## automation

A running NaviTui exposes a small local control API over a unix socket, so you
can drive it from anywhere:

```sh
navitui-remote status          # what's playing
navitui-remote next            # skip
navitui-remote play "daft punk" # search + play the top hit
```

Bind those to global hotkeys, or point an MCP client at `navitui-mcp` to let an
agent search, queue, download, and report now-playing. The API is localhost /
socket only and never leaves your machine.

## the suite

- [**ricekit**](https://github.com/Gheat1/ricekit) — the design system this is built on
- [**ltui**](https://github.com/Gheat1/ltui) — a fast, beautiful TUI for Linear

## license

[GPL-3.0-or-later](LICENSE) — made by [@Gheat1](https://github.com/Gheat1).
Releases up to 0.3.0 were MIT; from 0.4.0 NaviTui is GPL so forks stay
open and keep their notices.
