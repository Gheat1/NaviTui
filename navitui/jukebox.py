"""Jukebox mode — drive the SERVER's own audio output.

For a headless Navidrome/Subsonic box wired to speakers: instead of streaming
to this machine and decoding locally (the default `player.Player`), NaviTui
tells the server to play, pause, seek and set its own gain via the Subsonic
`jukeboxControl` endpoint. Sound comes out of the server, not here.

`JukeboxPlayer` satisfies the exact same duck-typed interface the app uses for
the local mpv player (play/stop/pause/seek/volume/…), so the rest of the app
"just works" once `create_player` hands one back. There is no mpv thread here:
network calls run on the app's asyncio loop (kicked off from the UI thread with
`call_soon_threadsafe`), and position/duration are POLLED from jukebox status on
the app's existing heartbeat — no new per-widget timer.

Interface gaps (documented deferrals, see JukeboxPlayer.play):
  * `level` (audio loudness for the visualizer) is always 0.0 — the server
    exposes no meter, so the widgets fall back to their faked motion.
  * position is polled (~1s granularity via `status`) and extrapolated between
    polls, so it is less precise than mpv's `time-pos`.
"""

from __future__ import annotations

import asyncio
from typing import Callable
from urllib.parse import parse_qs, urlparse

# how often to hit `jukeboxControl?action=status` for a fresh position/gain.
# The heartbeat runs at 8fps; polling every second keeps the network quiet
# while the UI extrapolates position smoothly between polls.
_POLL_INTERVAL = 1.0


def _song_id_from_source(source: str) -> str | None:
    """Pull the Subsonic song id out of whatever `play()` was handed.

    The app plays either a `stream_url` (…/rest/stream?id=<id>&…) or a local
    pinned file path. Jukebox needs the id, so we recover it from the stream
    URL's query string; a bare id (no `?`) is accepted as-is. A local file path
    has no id the server can play → None (caller falls back to local)."""
    if "id=" not in source:
        return source if "://" not in source and "/" not in source else None
    try:
        qs = parse_qs(urlparse(source).query)
    except ValueError:
        return None
    ids = qs.get("id")
    return ids[0] if ids else None


class JukeboxPlayer:
    """Server-side playback via `jukeboxControl`, same shape as `player.Player`.

    Callbacks mirror the local player exactly (fired on the asyncio loop, never
    a foreign thread — so the app can keep using them directly):
      on_position(pos_seconds, duration_seconds)
      on_track_end(failed: bool)

    `client.jukebox_control(action, **params)` is awaited for every command;
    `loop` schedules those coroutines from the (synchronous) transport methods
    the app calls. `poll` is driven by the app heartbeat.
    """

    def __init__(
        self,
        on_position: Callable[[float, float], None],
        on_track_end: Callable[[bool], None],
        client,
        loop: asyncio.AbstractEventLoop,
        on_unsupported: Callable[[str], None] | None = None,
    ) -> None:
        self._on_position = on_position
        self._on_track_end = on_track_end
        self._client = client
        self._loop = loop
        self._on_unsupported = on_unsupported

        self.position = 0.0
        self.duration = 0.0
        self._want_playing = False
        self._paused = True
        self._muted = False
        self._volume = 80
        self._gain_before_mute = 80  # restore point for unmute
        self.level = 0.0  # no server-side meter — visualizer uses faked motion

        self._closing = False
        self._last_poll = 0.0  # loop.time() of the last real status reconcile
        self._last_status_pos = 0.0  # server-reported position at that reconcile
        self._poll_inflight = False
        self._ended = False  # guard so a drained status fires on_track_end once

    # ── async command plumbing ────────────────────────────────────────
    def _dispatch(self, coro) -> None:
        """Fire-and-forget a jukebox command onto the app loop from a sync
        transport call. Never blocks the UI; failures fall through to the
        error handler which degrades to local playback."""
        if self._closing:
            return
        try:
            self._loop.call_soon_threadsafe(lambda: self._loop.create_task(self._guard(coro)))
        except Exception:
            pass

    async def _guard(self, coro) -> None:
        try:
            await coro
        except Exception as e:
            if not self._closing and self._on_unsupported is not None:
                self._on_unsupported(str(e))

    # ── transport ─────────────────────────────────────────────────────
    def play(self, url: str, start: float = 0.0) -> None:
        song_id = _song_id_from_source(url)
        self.position = start
        # duration is fed by the app via set_duration() right before this call
        # (jukebox status doesn't report it), so we must NOT clear it here
        self._last_status_pos = start
        self._paused = False
        self._ended = False
        if song_id is None:
            # a local pinned file / unrecognized source — the server can't play
            # it. Treat as a failed load so the app moves on, but defer the
            # callback to the loop so we never re-enter the app from inside
            # play() (the app override normally hands us a stream URL, so this
            # is just a safety net).
            self._want_playing = False
            try:
                self._loop.call_soon(self._on_track_end, True)
            except Exception:
                pass
            return
        self._want_playing = True

        async def _load() -> None:
            # replace the whole jukebox playlist with this one track, then start
            await self._client.jukebox_control("set", id=song_id)
            if start > 0:
                await self._client.jukebox_control("skip", index=0, offset=int(start))
            await self._client.jukebox_control("start")

        self._dispatch(_load())

    def stop(self) -> None:
        self._want_playing = False
        self._paused = True
        self.position = 0.0
        self.duration = 0.0
        self._dispatch(self._client.jukebox_control("stop"))
        self._dispatch(self._client.jukebox_control("clear"))

    @property
    def paused(self) -> bool:
        return self._paused

    def set_paused(self, paused: bool) -> None:
        if paused == self._paused:
            return
        self._paused = paused
        self._dispatch(self._client.jukebox_control("stop" if paused else "start"))

    def toggle_pause(self) -> None:
        self.set_paused(not self._paused)

    @property
    def active(self) -> bool:
        return self._want_playing

    def seek(self, seconds: float) -> None:
        if not self._want_playing:
            return
        target = self.position + seconds
        self._seek_to_seconds(target)

    def seek_to(self, fraction: float) -> None:
        if not self._want_playing or self.duration <= 0:
            return
        self._seek_to_seconds(max(0.0, min(fraction, 0.99)) * self.duration)

    def _seek_to_seconds(self, seconds: float) -> None:
        seconds = max(0.0, seconds)
        if self.duration > 0:
            seconds = min(seconds, self.duration)
        self.position = seconds
        self._last_status_pos = seconds
        # jukebox seeks by (playlist index, offset in whole seconds)
        self._dispatch(self._client.jukebox_control("skip", index=0, offset=int(seconds)))

    # ── volume (jukebox gain is 0.0–1.0; the app speaks 0–130) ─────────
    @property
    def volume(self) -> int:
        return self._volume

    def set_volume(self, value: int) -> int:
        value = max(0, min(130, value))
        self._volume = value
        if not self._muted:
            self._push_gain(value)
        return value

    def _push_gain(self, value: int) -> None:
        gain = max(0.0, min(1.0, value / 100.0))  # 100 == unity; >100 clamps
        self._dispatch(self._client.jukebox_control("setGain", gain=gain))

    @property
    def muted(self) -> bool:
        return self._muted

    def toggle_mute(self) -> bool:
        self._muted = not self._muted
        if self._muted:
            self._gain_before_mute = self._volume
            self._push_gain(0)
        else:
            self._push_gain(self._gain_before_mute)
        return self._muted

    # ── status polling (driven by the app heartbeat) ──────────────────
    def poll(self) -> None:
        """Advance/refresh position. Called every heartbeat tick.

        Between server polls we extrapolate position locally (so the progress
        bar moves at 8fps, not 1fps); every `_POLL_INTERVAL` we reconcile with
        a real `status` call which also detects natural end-of-track."""
        if self._closing or not self._want_playing:
            return
        now = self._loop.time()
        # local extrapolation while playing keeps the UI lively between polls:
        # position = server-reported position + seconds since that reconcile
        if not self._paused and self._last_poll:
            self.position = self._last_status_pos + max(0.0, now - self._last_poll)
            if self.duration > 0:
                self.position = min(self.position, self.duration)
        if (now - self._last_poll >= _POLL_INTERVAL or not self._last_poll) and not self._poll_inflight:
            self._poll_inflight = True
            self._loop.create_task(self._guard(self._refresh_status()))
        self._on_position(self.position, self.duration)

    async def _refresh_status(self) -> None:
        try:
            status = await self._client.jukebox_control("status")
        finally:
            self._poll_inflight = False
            self._last_poll = self._loop.time()  # re-anchor extrapolation
        if self._closing or not self._want_playing:
            return
        jb = status.get("jukeboxStatus", status.get("jukeboxPlaylist", {})) if isinstance(status, dict) else {}
        pos = jb.get("position")
        if pos is not None:
            try:
                self._last_status_pos = float(pos)
                self.position = self._last_status_pos
            except (TypeError, ValueError):
                pass
        playing = jb.get("playing")
        if playing is not None:
            self._paused = not bool(playing)
        # natural end: the server reports it stopped while we still wanted to
        # play and we've run out the clock. Fire on_track_end once.
        if playing is False and self.duration > 0 and self.position >= self.duration - 1.5:
            if not self._ended:
                self._ended = True
                self._want_playing = False
                self._on_track_end(False)

    def set_duration(self, duration: float) -> None:
        """The app knows each song's duration from the library metadata; it
        feeds it in here so progress/seek math has a length to work with
        (jukebox status doesn't reliably report track duration)."""
        try:
            self.duration = max(0.0, float(duration))
        except (TypeError, ValueError):
            self.duration = 0.0

    def terminate(self) -> None:
        self._closing = True
        self._want_playing = False
        # best-effort: stop the server output so it doesn't keep playing after
        # the TUI exits. Fire-and-forget; the loop is about to close.
        try:
            self._loop.call_soon_threadsafe(
                lambda: self._loop.create_task(self._guard(self._client.jukebox_control("stop")))
            )
        except Exception:
            pass
