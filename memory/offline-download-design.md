---
name: offline-download-design
description: Design sketch for NaviTui's offline audio download/cache subsystem — the user's most-wanted feature
metadata:
  type: project
---

The user's top-wanted feature (2026-07-08): download audio for offline play — "download everything or a playlist." Not yet built. Part of [[feature-roadmap]].

**Why:** NaviTui's whole pitch is cache-first / never-block-on-network, but only cover art is cached today (`SubsonicClient.cached_art`/`cover_art` in api.py); audio always streams via `stream_url`. Offline audio is the natural completion of that pitch and a real moat vs every streaming TUI.

**How to apply (sketch):**
- Mirror the art-cache pattern: a `_audio_dir` under the app cache; `cached_stream(song_id)` returns a local path if present, else None. Download via the Subsonic `download` (original file) or `stream` (transcoded) endpoint to `.part` then atomic rename (same as `cover_art`).
- `stream_url` (or `_play_current`) checks the local file FIRST, falls back to network URL. mpv plays a local path transparently.
- Actions: "download this track / playlist / album", and "download everything." Background worker (textual `@work`, its own group) with the 8fps spinner already used for lib/songs refresh.
- Downloads panel / progress UI showing per-item progress; a storage budget with LRU eviction so it doesn't fill the disk.
- Auto-download options: pin starred, prefetch next-in-queue while playing (also helps gapless).
- "Offline mode" toggle: play only downloaded tracks; auto-detect when server unreachable and degrade gracefully (the app already shows cached library when offline via `_connection_trouble`).
- Pair with an **offline mutation queue** so stars/ratings/scrobbles made offline flush on reconnect (starring is already optimistic against the cache).
- Store download manifest in the existing state/cache dir (AppDirs) so pins survive restart.
