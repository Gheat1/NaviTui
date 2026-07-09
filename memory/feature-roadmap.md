---
name: feature-roadmap
description: Brainstormed feature ideas to make NaviTui beat competing Subsonic/Navidrome TUIs; user liked all, esp. offline downloads
metadata:
  type: project
---

Ideas to make NaviTui beat ncspot / spotify-player / stmp / supersonic. User (2026-07-08) endorsed all of these and especially wants the **offline download** feature ("download everything or a playlist"). A second agent was mid-task when these were generated — none implemented yet; verify against current code before building.

**Why:** Most easy wins are already done (MPRIS, Discord presence, notifications, scrobble, ratings, sharing, lyrics modal, resume-queue, ReplayGain, gapless). Differentiation now needs signature features.
**How to apply:** Fit the 3 priorities — fast (cache-first), alive (one 8fps heartbeat), pretty (ricekit palette at render time). Reuse existing machinery (art-cache pattern in api.py, `advance()` in playqueue, heartbeat/`now._tick`).

TIER 1 (signature):
- Synced/karaoke lyrics via OpenSubsonic `getLyricsBySongId` (timed lines), scroll+highlight off heartbeat. Upgrade existing plain `get_lyrics`/`LyricsModal`.
- **Offline audio cache/download** (top priority) — extend the art-cache idea to audio. See [[offline-download-design]].
- Endless radio / instant mix — when queue drains in `_on_track_end`, pull `getSimilarSongs2`/`getTopSongs` and keep playing; "start radio from track/artist" action.
- Type-to-filter in tracks pane (instant in-pane fuzzy narrowing, no network).

TIER 2 (real gaps):
- Full playlist editing: reorder/remove/rename/delete + "save current queue as playlist" (`createPlaylist` with queue IDs). 0.3.0 tagline promises editable playlists — currently only create+append.
- Multi-select bulk actions (queue/playlist-add/star N rows).
- Sleep timer + playback speed (mpv `speed`).
- Bitrate/transcode cap (`stream` maxBitRate/format) for low bandwidth.
- Genre/year filtering; bookmarks (`getBookmarks`) for long tracks/audiobooks.

TIER 3 (flourishes/stretch):
- Real audio-reactive visualizer from mpv live levels (replace faked anim).
- Command palette (Textual built-in) for all actions.
- Click-to-seek progress bar (seek_fraction likely already exists — check wiring).
- ListenBrainz / multi-scrobble; multiple server profiles / account switch.
- Crossfade + prefetch next stream for bulletproof gapless.

NEW ideas (second round):
- **Album-art-derived theming**: extract dominant colors from cover → tint ricekit accent live per track. Huge "pretty" wow, on-ethos.
- **Zen / now-playing splash mode**: hide panels, big centered cover + synced lyrics.
- **Offline mutation queue**: buffer stars/ratings/scrobbles while offline (starring is already optimistic), flush on reconnect. Essential companion to offline mode.
- **Local listening stats / mini-wrapped** from a local play log ("top tracks this week", heatmap).
- Discord upgrade: add `start`/`end` timestamps (live progress bar in Discord) + album-art large_image + share button. integrations.py currently text-only.
- notify-send action buttons (next/pause); MPRIS already covers media keys.
- Podcasts (`getPodcasts`/episodes/download) + internet radio (`getInternetRadioStations`).
- Jukebox mode (server-side audio out) for headless-server-with-speakers setups.
- CLI remote control of a running instance via MPRIS/socket (`navitui next`, `navitui play "query"`).
- Smart playlists / saved filters; full-text lyric search; duplicate finder / library stats.
- Vim-style repeat counts (3j), macros.
- Export now-playing card as SVG (reuse screenshot infra in tools/).

EXTERNAL CONTROL (user especially excited, 2026-07-08):
- **MCP server** — expose NaviTui as MCP tools (play/pause/next/queue/search/now-playing/download) so Claude or other agents can control the player. Novel for a TUI music player; nothing in the space has it.
- **Remote-control API** — local HTTP/WebSocket (or unix socket) exposing transport + queue + now-playing state; the CLI remote-control and MCP server both sit on top of it. "Control it from other places." Exact shape TBD with user.

All ideas filed as GitHub issues on Gheat1/NaviTui assigned to Gheat1 (2026-07-08), no AI attribution per hard rule.
