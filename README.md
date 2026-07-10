<div align="center">

# ♪ NaviTui

**A fast, animated terminal player for [Navidrome](https://www.navidrome.org/) and any Subsonic server.**

Real cover art in the terminal · playback through mpv · offline downloads ·
karaoke lyrics · endless radio · a control API for scripts and AI agents ·
five live-previewed themes — and everything moves.

<img src="assets/shot-main.png" alt="NaviTui now playing" width="100%">

[![license](https://img.shields.io/badge/license-GPL--3.0--or--later-blue)](LICENSE)
[![python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![built with textual](https://img.shields.io/badge/built%20with-Textual-5a3fd6)](https://github.com/Textualize/textual)
[![design system](https://img.shields.io/badge/design-ricekit-e8a33d)](https://github.com/Gheat1/ricekit)

</div>

---

## contents

- [Why NaviTui](#why-navitui)
- [Screenshots](#screenshots)
- [Features](#features)
- [Install](#install)
- [First run](#first-run)
- [Keybindings](#keybindings)
- [Configuration](#configuration)
- [Automation & the control API](#automation--the-control-api)
- [How it works](#how-it-works)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [The suite](#the-suite)
- [License](#license)
- [Acknowledgements](#acknowledgements)

---

## Why NaviTui

If you self-host your music on **Navidrome** (or any Subsonic-compatible server)
and you live in a terminal, your options are usually a bare-bones TUI or a full
desktop GUI. NaviTui aims for the sweet spot: a player that's **as quick and
keyboard-driven as a TUI should be, but as rich and good-looking as a GUI** —
real album art, fluid animation, offline downloads, and an automation surface
none of the others have.

It is built for one workflow above all: **just play my songs.** No album grid to
click through, no "now browse this artist's discography" ceremony. One sidebar of
ways to list tracks, one big track list, press enter. Albums and artists still
exist — they live inside search, where you go looking for them on purpose.

**What sets it apart**

- **Cover art as actual pixels** — kitty graphics / sixel where your terminal
  supports it, gracefully degrading to truecolor half-cells and then unicode.
- **Offline-first, done properly** — pin a track, a playlist, or your whole
  library to disk; an offline mode that skips the network entirely; and an
  offline **mutation queue** so the stars and scrobbles you make on a plane sync
  when you land.
- **A control API + MCP server** — drive a running player from a shell script,
  a global hotkey, or an AI agent. Nothing else in this space ships that.
- **It feels alive** — a shimmering wordmark, a visualizer that reacts to real
  playback loudness, a sub-cell progress bar that breathes — all from a single
  8fps heartbeat that repaints only a handful of cells, so it stays cheap.

Cross-platform: playback works on **Linux, macOS, and Windows** (anywhere
`libmpv` runs). Native now-playing rides the OS integration on each — **MPRIS2**
on Linux, **MPNowPlayingInfoCenter / Control Center** on macOS, and the **System
Media Transport Controls** on Windows — so track, art, and progress show up
wherever your OS puts them. (Metadata display is solid across all three;
media-*key* delivery on macOS/Windows is new and still wants on-target testing.)

---

## Screenshots

<div align="center">

**Search — everything, grouped, instantly**

<img src="assets/shot-search.png" alt="global search" width="80%">

**`void` theme — OLED black, art glowing**

<img src="assets/shot-void.png" alt="void theme" width="80%">

**Synced, scrolling lyrics**

<img src="assets/shot-lyrics.svg" alt="synced karaoke lyrics" width="70%">

</div>

---

## Features

### Songs-first library
- One sidebar: **all tracks**, **recently added**, **recently played**, **most
  played**, **starred**, **shuffle everything** — your **playlists** right under
  it, plus **genres** (`y`), **podcasts**, and **internet radio**.
- Albums and artists live inside **search** (`/`), where they belong; jump from
  any track to its album (`e`) or artist (`E`).
- **Type-to-filter** (`\`) narrows the current list as you type — no network
  round-trip, instant.
- **Multi-select** (`v`) — tag rows, then queue, playlist-add, star, or download
  the whole selection at once.

### Playback & the queue
- A queue that shows what's **up next** — played tracks dim above the line;
  scroll up for history. Add (`a`), play-next (`A`), remove (`x`), clear (`X`),
  reorder (`ctrl+↑/↓`).
- **Shuffle** that reshuffles the up-next and starts a fresh view on a random
  track; **repeat** off / all / one.
- **Sleep timer** (`<`) and **playback speed** (`>`) — great for podcasts and
  audiobooks; **crossfade** with next-track prefetch for seamless transitions.
- The queue — **including your position inside the current song** — survives a
  restart.

### Offline
- **Download** a track (`d`), a whole view or playlist (`D`), or your **entire
  library** (`ctrl+d`). Pinned tracks get a ✓ and play locally forever.
- **Offline mode** (`O`) plays only what's downloaded and skips the network.
- Stars / ratings / scrobbles made offline are **queued and flushed** on
  reconnect.
- A streaming **bitrate cap** (`Q`) for tight connections — downloads always
  keep the original file.

### Karaoke lyrics
- Timed, scrolling, highlighted lyrics (`L`) that ride the beat — and the same
  view in **zen mode** (`z`): a big centred cover-and-lyrics splash.

### Discovery
- **Endless radio** — start a station from any track or artist (`i`), or let the
  queue **autoplay similar tracks forever** when it drains (`I`).
- **Bookmarks** (`w` / `W`) to save and jump back to a spot in long tracks.
- **Listening stats** (`ctrl+w`) — a local mini-wrapped: top tracks & artists,
  this-week counts, an activity sparkline, streaks.

### Part of your desktop
- Native now-playing on every platform: **MPRIS2** (Linux — media keys, waybar,
  playerctl), **Control Center / lock screen** (macOS), **System Media Transport
  Controls** (Windows). Install `navitui[macos]` or `navitui[windows]` for those.
- **Desktop notifications** on track change (cover as the icon) with **prev /
  play-pause / next** action buttons.
- Optional **Discord rich presence** with a live progress bar.
- Optional **ListenBrainz** scrobbling, alongside your server's own scrobble.

### Control & automation
- A local **control API** over a unix socket, so other tools can drive a running
  player. It powers:
  - **`navitui-remote`** — a scriptable CLI (`navitui-remote status`, `next`,
    `play "<query>"`, …), perfect for global hotkeys.
  - **`navitui-mcp`** — an MCP server so Claude and other agents can control
    playback, search, download, and read what's now playing.
- **Jukebox mode** (`J`) — drive the **server's own** audio output, for a
  headless box wired to real speakers.

### Playlists you can actually edit
- Create, add (`p`), remove (`P`), reorder (`shift+↑/↓`), rename (`ctrl+r`),
  delete (`ctrl+x`) — or **save the current queue as a playlist** (`ctrl+s`).

### Alive & pretty
- The wordmark shimmers, the **visualizer reacts to real playback loudness**,
  the progress bar has 1/8-cell resolution and breathes, long titles marquee,
  panels fade in — all from one 8fps heartbeat.
- **Album-art theming** tints the UI toward the current cover's colors.
- **Five themes**, live-previewed (`t` cycles, `T` picks) — including `clear`
  (your terminal's transparency shows through) and `system` (your terminal's own
  ANSI palette).
- **Cache-first** — every pane renders instantly from disk, then refreshes
  silently in the background (auto every 3 minutes).

### And the rest
- **Scrobbles, stars & ratings** — star (`f`), rate 1–5 on the number row.
- **Command palette** (`ctrl+p`) — every action, fuzzy-searchable.
- **Export** the now-playing card as an SVG (`C`); copy a **share link** (`S`).
- **Vim repeat counts** (`3j` moves down three) and full **mouse support** —
  click the progress bar to seek, drag the panel dividers, click shuffle/repeat.

---

## Install

You need **libmpv** for playback (everything else ships with the package):

```sh
# arch
sudo pacman -S mpv
# debian / ubuntu
sudo apt install libmpv2
# fedora
sudo dnf install mpv-libs
# macos
brew install mpv
# windows — put libmpv-2.dll on your PATH: https://mpv.io/installation/
```

Then install NaviTui. [`uv`](https://github.com/astral-sh/uv) is the easy path:

```sh
uv tool install "navitui[integrations] @ git+https://github.com/Gheat1/NaviTui"
navitui
```

or with pip:

```sh
pip install "navitui[integrations] @ git+https://github.com/Gheat1/NaviTui"
```

**Optional extras**

| extra | pulls in | for |
| --- | --- | --- |
| `integrations` | `dbus-fast`, `pypresence` | MPRIS2 media keys (Linux), Discord presence |
| `macos` | `pyobjc-framework-MediaPlayer`, `pyobjc-framework-Cocoa` | Control Center now-playing (macOS) |
| `windows` | `winrt-runtime`, `winrt-Windows.*` | System Media Transport Controls (Windows) |
| `mcp` | `mcp` | the `navitui-mcp` agent server |

Combine them: `navitui[integrations,mcp]`. Drop the extras entirely for the bare
player — every integration degrades to a clean no-op when its dependency is
absent, so nothing breaks.

---

## First run

On first launch NaviTui asks for your **server URL, username, and password**,
validates them live, and stores a **salted token** (never the password itself)
in `~/.config/navitui/`, `chmod 600`. It also drops a commented
`player.toml` next to it for you to tweak later.

Works with **Navidrome** and any **Subsonic / OpenSubsonic**-compatible server
(Gonic, Airsonic, …). No server of your own yet? Try it against the public demo:

```
server:    https://demo.navidrome.org
username:  demo
password:  demo
```

---

## Keybindings

Press `?` in-app for a live cheatsheet, or `ctrl+p` to fuzzy-search every action
by name. Every key here is remappable — see [Configuration](#configuration).
Lists are vim-navigable (`j` / `k` / `g` / `G`) and accept **repeat counts**
(`5j`).

**Playback & transport**

| key | action | key | action |
| --- | --- | --- | --- |
| `space` | play / pause | `s` | shuffle |
| `n` / `b` | next / previous | `r` | repeat (off → all → one) |
| `←` / `→` | seek 5s | `m` | mute |
| `shift+←/→` | seek 30s | `>` | playback speed |
| `-` / `+` | volume down / up | `<` | sleep timer |

**Queue**

| key | action | key | action |
| --- | --- | --- | --- |
| `a` | add to queue | `ctrl+↑` / `ctrl+↓` | move track up / down |
| `A` | play next | `X` | clear queue |
| `x` | remove track | `ctrl+s` | save queue as playlist |

**Library & navigation**

| key | action | key | action |
| --- | --- | --- | --- |
| `/` | search everything | `e` / `E` | go to album / artist |
| `\` | filter current list | `y` | browse by genre |
| `v` | multi-select mode | `R` | refresh from server |
| `h` / `l` | previous / next panel | `j`/`k`/`g`/`G` | move in lists |

**Playlists**

| key | action | key | action |
| --- | --- | --- | --- |
| `p` | add to a playlist | `shift+↑/↓` | reorder in playlist |
| `P` | remove from playlist | `ctrl+r` | rename playlist |
| `ctrl+s` | save queue as playlist | `ctrl+x` | delete playlist |

**Offline & quality**

| key | action | key | action |
| --- | --- | --- | --- |
| `d` | download track | `O` | offline mode |
| `D` | download view / playlist | `Q` | cycle streaming quality |
| `ctrl+d` | download whole library | `J` | jukebox (server audio) mode |

**Discovery & now-playing**

| key | action | key | action |
| --- | --- | --- | --- |
| `i` | start radio from here | `L` | lyrics |
| `I` | endless-autoplay toggle | `z` | zen splash |
| `w` / `W` | set / list bookmarks | `ctrl+w` | listening stats |
| `f` | star / unstar | `1`–`5` | rate (same digit clears) |
| `S` | copy share link | `C` | export now-playing card (SVG) |

**App**

| key | action | key | action |
| --- | --- | --- | --- |
| `t` / `T` | cycle / pick theme | `N` | toggle notifications |
| `ctrl+p` | command palette | `?` | help |
| `R` | refresh | `q` | quit |

---

## Configuration

NaviTui writes a fully-commented `~/.config/navitui/player.toml` on first run.
Edit it and **restart** to apply. Everything is optional and safe to delete —
missing keys fall back to defaults, and a malformed file is ignored rather than
fatal.

```toml
# ── playback ──────────────────────────────────────────────
replaygain    = "album"   # "album" | "track" | "no"
gapless       = "weak"    # "yes" | "weak" (gapless when formats match) | "no"
crossfade     = 0.0       # seconds of soft fade on track change (0 = off)
max_bitrate   = 0         # streaming cap in kbps (0 = original / unlimited)
stream_format = ""        # transcode target: "mp3" | "opus" | "raw" | "" original

# ── desktop & scrobbling ─────────────────────────────────
notifications         = true    # desktop notification on track change
art_theming           = true    # tint the UI toward the cover's colors
discord_rich_presence = false
discord_app_id        = ""       # from discord.com/developers/applications
listenbrainz_token    = ""       # from listenbrainz.org/profile

# ── control & server audio ───────────────────────────────
remote_control = true     # local control API (unix socket) for the CLI & MCP
remote_token   = ""       # optional shared secret (required on the TCP fallback)
jukebox        = false    # play on the SERVER's audio output, not this machine

# ── remap any key ────────────────────────────────────────
[keybinds]
# action_id = "key"  — comma-separate aliases, e.g. "plus,equals_sign"
next_track = "ctrl+n"
download   = "d"
# …every action listed by `?` can be rebound here
```

- **`replaygain` / `gapless` / `crossfade`** are handed straight to mpv, so they
  behave exactly as mpv's own options do.
- **`max_bitrate` / `stream_format`** apply only to network streams — **offline
  downloads always keep the original file**. Cycle presets at runtime with `Q`.
- **`remote_control`** exposes the socket the CLI and MCP server talk to; it is
  **localhost / socket only** and never leaves your machine. Set `remote_token`
  to require a shared secret.

---

## Automation & the control API

A running NaviTui exposes a small local control API over a unix socket, so you
can drive it from anywhere on the same machine.

**From the shell** (great for global hotkeys):

```sh
navitui-remote status              # what's playing, as JSON
navitui-remote play-pause          # toggle
navitui-remote next                # skip
navitui-remote play "daft punk"    # search + play the top hit
navitui-remote volume 80           # set volume
```

**From an AI agent** — point any MCP client at `navitui-mcp`:

```jsonc
// e.g. Claude Code / any MCP host
{
  "mcpServers": {
    "navitui": { "command": "navitui-mcp" }
  }
}
```

The agent can then search your library, queue and download tracks, control
playback, and report what's now playing. The transport is the same local socket
— nothing is exposed off your machine.

---

## How it works

Three principles, in order:

1. **Fast** — cache-first everywhere. Each pane renders the last-known state from
   disk in ~50ms, then a background worker fetches fresh rows and swaps them in
   silently. Mutations update the cache immediately, so what you see is always
   what you did — even if you quit before the refresh lands.
2. **Alive** — one shared 8fps heartbeat drives *every* animation (logo shimmer,
   visualizer, progress pulse, marquee, spinners). Each tick repaints only a few
   cells, so constant motion costs almost nothing and never fights your CPU.
3. **Pretty** — rounded borders, a disciplined color palette, and nerd-font
   icons, all from [**ricekit**](https://github.com/Gheat1/ricekit), the design
   system NaviTui shares with the rest of its suite.

Under the hood: [Textual](https://github.com/Textualize/textual) for the UI,
[`python-mpv`](https://github.com/jaseg/python-mpv) (libmpv) for playback,
[`textual-image`](https://github.com/lnqs/textual-image) for terminal-native
cover art, and an async [httpx](https://www.python-httpx.org/) Subsonic client.
Auth is the salted-token scheme — the password is turned into a per-session
token and never written to disk.

---

## Troubleshooting

**"libmpv not found" / no sound.** NaviTui still runs (you can browse, search,
queue) but can't play audio. Install mpv for your OS — see [Install](#install).
On Windows, `libmpv-2.dll` must be on your `PATH`.

**Cover art shows as blocks instead of a real image.** Your terminal (or a
multiplexer like tmux) isn't advertising a graphics protocol, so NaviTui fell
back to half-cells. Force a protocol with the `NAVITUI_ART` env var:

```sh
NAVITUI_ART=auto      # detect the best available (default)
NAVITUI_ART=tgp       # force the kitty graphics protocol
NAVITUI_ART=sixel     # force sixel
NAVITUI_ART=halfcell  # truecolor half-blocks (works nearly everywhere)
NAVITUI_ART=unicode   # last-resort unicode
NAVITUI_ART=off       # a tasteful placeholder, no image
```

kitty, WezTerm, Ghostty, and Konsole all render real images; inside tmux you may
need `NAVITUI_ART=tgp`.

**Media keys do nothing.** On Linux they ride on MPRIS2 — install the
`integrations` extra (`dbus-fast`) and be on a session bus; `playerctl status`
should report NaviTui while it's playing. On macOS install `navitui[macos]`
(now-playing appears in Control Center); on Windows install `navitui[windows]`
(the SMTC overlay). Metadata shows reliably on all three — if the hardware media
*keys* still don't reach NaviTui on macOS/Windows, that path is new and being
hardened, so file an issue with your terminal and OS version.

**Nothing loads / it says it's offline.** NaviTui shows your last cached library
when it can't reach the server, so you can still play downloaded tracks. Check
the server URL in `~/.config/navitui/`, or press `R` to retry.

**A key is bound to something I don't want.** Rebind it under `[keybinds]` in
`player.toml` and restart — see [Configuration](#configuration).

---

## Development

```sh
git clone https://github.com/Gheat1/NaviTui
cd NaviTui
python -m venv .venv && . .venv/bin/activate
pip install -e ".[integrations,mcp]"
navitui
```

Tests run headless against a mocked client, or read-only against the public demo
server. Isolate state with `HOME=$(mktemp -d)` and pass `ao="null"` so mpv needs
no audio device. README screenshots are generated, never captured from a real
library — `tools/shots.sh` drives the real app in kitty; `tools/screenshots.py`
is the headless fallback. See [`CLAUDE.md`](CLAUDE.md) for the architecture map
and the hard-won sharp-edges table.

Issues and PRs welcome. NaviTui is GPL-3.0-or-later, so contributions and forks
stay open (see [License](#license)).

---

## The suite

- [**ricekit**](https://github.com/Gheat1/ricekit) — the Textual design system NaviTui is built on
- [**ltui**](https://github.com/Gheat1/ltui) — a fast, beautiful TUI for Linear, where ricekit came from

---

## License

[**GPL-3.0-or-later**](LICENSE) — made by [@Gheat1](https://github.com/Gheat1).

Releases up to **0.3.0** were MIT; from **0.4.0** NaviTui is GPL, so derivatives
stay open source and keep their notices. (Copies of the earlier MIT releases
remain MIT — that can't be retracted — but everything current is copyleft.)

---

## Acknowledgements

Built on the shoulders of [Textual](https://github.com/Textualize/textual),
[mpv](https://mpv.io/), [Navidrome](https://www.navidrome.org/) and the
[Subsonic API](https://www.subsonic.org/pages/api.jsp),
[textual-image](https://github.com/lnqs/textual-image), and
[httpx](https://www.python-httpx.org/) — with cover-art protocols courtesy of the
[kitty](https://sw.kovidgoyal.net/kitty/graphics-protocol/) and sixel graphics
standards. Thank you to everyone who self-hosts their music and keeps the
ecosystem alive.

<div align="center">

*If NaviTui makes your library feel like yours again, drop it a ⭐.*

</div>
