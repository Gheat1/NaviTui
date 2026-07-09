"""Playback engine — a thin, thread-aware wrapper around libmpv.

mpv does the heavy lifting (HTTP streaming, every codec, seeking, volume);
we observe `time-pos`/`duration` and the `end-file` event. mpv fires those
callbacks on its own event thread, so the app schedules UI work with
`loop.call_soon_threadsafe` (never a blocking call — that deadlocks against
`terminate()`); this module never touches the UI.

If libmpv isn't installed the app still runs (browse, search, queue); it
just tells you how to get sound on your OS.
"""

from __future__ import annotations

import asyncio
from typing import Callable

MPV_AVAILABLE = True
MPV_ERROR = ""
try:
    import mpv as _mpv
except (ImportError, OSError, AttributeError) as e:  # missing libmpv shows as OSError
    MPV_AVAILABLE = False
    MPV_ERROR = str(e)

INSTALL_HINTS = (
    "libmpv not found — install mpv for playback:\n"
    "  arch:    sudo pacman -S mpv\n"
    "  debian:  sudo apt install libmpv2\n"
    "  macos:   brew install mpv\n"
    "  windows: place libmpv-2.dll on PATH (mpv.io/installation)"
)


class Player:
    """One mpv instance for the life of the app.

    Callbacks (all fired from mpv's event thread):
      on_position(pos_seconds, duration_seconds)
      on_track_end(failed: bool)   natural EOF or a stream error
    """

    def __init__(
        self,
        on_position: Callable[[float, float], None],
        on_track_end: Callable[[bool], None],
        ao: str | None = None,
        replaygain: str = "album",
        gapless: str = "weak",
    ) -> None:
        self._on_position = on_position
        self._on_track_end = on_track_end
        self.position = 0.0
        self.duration = 0.0
        self._want_playing = False
        self._closing = False
        self._last_forwarded = -1.0
        self._level = 0.0  # live audio loudness, ~0..1 (written from mpv's thread)

        opts: dict = dict(
            video=False,
            terminal=False,
            idle=True,
            audio_client_name="navitui",
        )
        if replaygain in ("album", "track"):
            opts["replaygain"] = replaygain
        if gapless in ("yes", "weak", "no"):
            opts["gapless_audio"] = gapless
        if ao:
            opts["ao"] = ao
        self._m = _mpv.MPV(**opts)

        # Attach an ebur128 loudness meter and observe its momentary level so
        # the visualizer can pump with real audio. Everything is feature-
        # detected: if the filter or metadata isn't available the level stays
        # at 0.0 and the widgets fall back to their faked motion.
        self._level_ok = False
        try:
            self._m.command("af", "add", "@nav:lavfi=[ebur128=metadata=1]")

            @self._m.property_observer("af-metadata/nav")
            def _loud(_name, value) -> None:
                if self._closing or not value:
                    return
                m = value.get("lavfi.r128.M")
                if m is None:
                    return
                try:
                    lufs = float(m)
                except (TypeError, ValueError):
                    return
                # ebur128 M ranges roughly -70 (silence) .. 0 LUFS; map the
                # useful -50..-5 band onto 0..1. Just store a float here —
                # never touch the UI from mpv's thread.
                self._level = max(0.0, min(1.0, (lufs + 50.0) / 45.0))
                self._level_ok = True

            self._level_ok = True
        except Exception:
            self._level_ok = False

        @self._m.property_observer("time-pos")
        def _time(_name, value) -> None:
            if value is None or self._closing:
                return
            self.position = float(value)
            # mpv fires this many times a second; only cross into the UI
            # thread on ~quarter-second boundaries
            if abs(self.position - self._last_forwarded) >= 0.25:
                self._last_forwarded = self.position
                self._on_position(self.position, self.duration)

        @self._m.property_observer("duration")
        def _dur(_name, value) -> None:
            if value is not None and not self._closing:
                self.duration = float(value)

        @self._m.event_callback("end-file")
        def _end(event) -> None:
            data = getattr(event, "data", None)
            reason = getattr(data, "reason", None)
            if not self._want_playing or self._closing:
                return  # we stopped/replaced it ourselves
            if reason == _mpv.MpvEventEndFile.EOF:
                self._want_playing = False
                self._on_track_end(False)
            elif reason == _mpv.MpvEventEndFile.ERROR:
                self._want_playing = False
                self._on_track_end(True)

    # ── transport ─────────────────────────────────────────────────────
    def play(self, url: str, start: float = 0.0) -> None:
        self._want_playing = False  # swallow the end-file of whatever was on
        self.position = start
        self.duration = 0.0
        self._last_forwarded = -1.0
        if start > 0:
            self._m.loadfile(url, start=str(start))
        else:
            self._m.loadfile(url)
        self._m.pause = False
        self._want_playing = True

    def stop(self) -> None:
        self._want_playing = False
        self._m.command("stop")
        self.position = 0.0
        self.duration = 0.0
        self._level = 0.0

    @property
    def paused(self) -> bool:
        return bool(self._m.pause)

    def set_paused(self, paused: bool) -> None:
        self._m.pause = paused

    def toggle_pause(self) -> None:
        self._m.pause = not self._m.pause

    @property
    def active(self) -> bool:
        """A track is loaded (playing or paused)."""
        return self._want_playing

    @property
    def level(self) -> float:
        """Live audio loudness in ~0..1, or 0.0 when no real signal is
        available (meter unsupported / nothing playing). Read on every
        heartbeat to drive the visualizer."""
        if not self._level_ok or not self._want_playing:
            return 0.0
        return self._level

    def seek(self, seconds: float) -> None:
        if not self._want_playing:
            return
        try:
            self._m.seek(seconds, reference="relative")
        except SystemError:
            pass  # seeking before the stream is ready

    def seek_to(self, fraction: float) -> None:
        if not self._want_playing or self.duration <= 0:
            return
        try:
            self._m.seek(max(0.0, min(fraction, 0.99)) * self.duration, reference="absolute")
        except SystemError:
            pass

    # ── volume ────────────────────────────────────────────────────────
    @property
    def volume(self) -> int:
        try:
            return int(self._m.volume or 0)
        except Exception:
            return 0

    def set_volume(self, value: int) -> int:
        value = max(0, min(130, value))
        self._m.volume = value
        return value

    async def fade_out(self, seconds: float) -> None:
        """Soft volume ramp to silence over `seconds`, then leave mpv muted-by-
        volume. A single mpv instance can't truly crossfade two streams, so this
        is the audible half of a soft transition; the caller loads the next
        track and calls `fade_in` to restore. Runs on the app's event loop (not
        mpv's thread), so awaiting between steps never blocks playback. Restores
        nothing on its own — `fade_in(base)` puts the user's volume back."""
        base = self.volume
        if seconds <= 0 or base <= 0:
            return
        steps = max(1, int(seconds / 0.05))
        for i in range(steps):
            try:
                self._m.volume = base * (1 - (i + 1) / steps)
            except Exception:
                return
            await asyncio.sleep(seconds / steps)

    async def fade_in(self, base: int, seconds: float) -> None:
        """Ramp volume from silence up to `base` (the user's set volume) over
        `seconds`, restoring it exactly at the end. Pairs with `fade_out`."""
        if seconds <= 0 or base <= 0:
            self.set_volume(base)
            return
        steps = max(1, int(seconds / 0.05))
        for i in range(steps):
            try:
                self._m.volume = base * (i + 1) / steps
            except Exception:
                break
            await asyncio.sleep(seconds / steps)
        self.set_volume(base)  # land on the exact user volume

    @property
    def muted(self) -> bool:
        return bool(self._m.mute)

    def toggle_mute(self) -> bool:
        self._m.mute = not self._m.mute
        return bool(self._m.mute)

    def terminate(self) -> None:
        self._closing = True  # observers go quiet before the core dies
        self._want_playing = False
        try:
            self._m.terminate()
        except Exception:
            pass


class NullPlayer:
    """Stands in when libmpv is missing so the rest of the app still works."""

    position = 0.0
    duration = 0.0
    paused = True
    active = False
    volume = 100
    muted = False
    level = 0.0

    def __init__(self, *a, **kw) -> None:
        pass

    def play(self, url: str, start: float = 0.0) -> None:
        pass

    def stop(self) -> None:
        pass

    def set_paused(self, paused: bool) -> None:
        pass

    def toggle_pause(self) -> None:
        pass

    def seek(self, seconds: float) -> None:
        pass

    def seek_to(self, fraction: float) -> None:
        pass

    def set_volume(self, value: int) -> int:
        return value

    async def fade_out(self, seconds: float) -> None:
        pass

    async def fade_in(self, base: int, seconds: float) -> None:
        pass

    def toggle_mute(self) -> bool:
        return False

    def terminate(self) -> None:
        pass


def create_player(on_position, on_track_end, ao: str | None = None, **opts):
    if not MPV_AVAILABLE:
        return NullPlayer()
    return Player(on_position, on_track_end, ao=ao, **opts)
