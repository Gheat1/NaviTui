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
import os
import sys
from typing import Callable


def _ensure_libmpv_on_search_path() -> None:
    """Make Homebrew's libmpv discoverable before `import mpv`.

    python-mpv locates libmpv via `ctypes.util.find_library('mpv')`, which on
    macOS only searches `~/lib`, `/usr/local/lib`, `/lib` and `/usr/lib`. On
    Apple Silicon, Homebrew installs libmpv under `/opt/homebrew/lib`, so a
    perfectly-installed `brew install mpv` is still invisible and playback
    silently drops to the "libmpv not found" hint. Prepend the Homebrew lib
    dirs to DYLD_FALLBACK_LIBRARY_PATH (find_library reads it at call time)
    so both Apple Silicon and Intel prefixes resolve.
    """
    if sys.platform != "darwin":
        return
    existing = os.environ.get("DYLD_FALLBACK_LIBRARY_PATH", "")
    parts = existing.split(os.pathsep) if existing else []
    # /opt/homebrew/lib: Apple Silicon; /usr/local/lib: Intel. The latter is
    # already a find_library default but harmless to restate — setting the var
    # replaces the default list, so keep it in.
    for lib_dir in ("/opt/homebrew/lib", "/usr/local/lib"):
        if os.path.isdir(lib_dir) and lib_dir not in parts:
            parts.append(lib_dir)
    if parts:
        os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = os.pathsep.join(parts)


_ensure_libmpv_on_search_path()

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

# How far short of the known duration an EOF must land to be treated as a
# truncated source (worth one resume) rather than a real track end. Wide enough
# to clear normal end-of-file jitter and encoder padding, tight enough that a
# stream cut a few seconds early is still caught.
_EOF_SHORT_TOLERANCE = 5.0


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
        replaygain_preamp: float = 0.0,
        replaygain_fallback: float = 0.0,
        audio_exclusive: bool = False,
        pipewire_buffer: int = 0,
    ) -> None:
        self._on_position = on_position
        self._on_track_end = on_track_end
        self.position = 0.0
        self.duration = 0.0
        self._want_playing = False
        self._closing = False
        self._last_forwarded = -1.0
        self._level = 0.0  # live audio loudness, ~0..1 (written from mpv's thread)
        self._url: str | None = None  # last source, for a premature-EOF resume
        self._retried = False  # one resume attempt is armed per play()

        opts: dict = dict(
            video=False,
            terminal=False,
            idle=True,
            audio_client_name="navitui",
        )
        if replaygain in ("album", "track"):
            opts["replaygain"] = replaygain
            # preamp/fallback only shape the ReplayGain path, so pass them
            # alongside the mode (mpv ignores them when replaygain is off).
            opts["replaygain_preamp"] = float(replaygain_preamp)
            opts["replaygain_fallback"] = float(replaygain_fallback)
        if gapless in ("yes", "weak", "no"):
            opts["gapless_audio"] = gapless
        if audio_exclusive:
            opts["audio_exclusive"] = True
        if pipewire_buffer and pipewire_buffer > 0:
            # mpv has no "pipewire buffer" knob; --audio-buffer (seconds) is the
            # real lever. Expose it in ms for convenience.
            opts["audio_buffer"] = pipewire_buffer / 1000.0
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
                # A dropped/truncated source (e.g. a network stream cut short by
                # a flaky link) surfaces as a *clean* EOF, indistinguishable
                # from a real finish — so the track "skips" with seconds still
                # on the clock. If we ended well short of the known length, try
                # to resume from here once before giving up and advancing.
                short = (
                    self.duration > 0.0
                    and self.position < self.duration - _EOF_SHORT_TOLERANCE
                )
                if short and not self._retried and self._url is not None:
                    self._retried = True
                    self._reload(self._url, max(0.0, self.position - 1.0))
                    return
                self._want_playing = False
                self._on_track_end(False)
            elif reason == _mpv.MpvEventEndFile.ERROR:
                self._want_playing = False
                self._on_track_end(True)

    # ── transport ─────────────────────────────────────────────────────
    def play(self, url: str, start: float = 0.0) -> None:
        # public entry: a genuine new track, so arm one resume attempt for it
        self._url = url
        self._retried = False
        self._reload(url, start)

    def _reload(self, url: str, start: float = 0.0) -> None:
        # (re)open `url` at `start`. Shared by play() and the premature-EOF
        # resume, which must NOT reset _retried (that would loop forever on a
        # source that always ends short).
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

    # ── speed ─────────────────────────────────────────────────────────
    @property
    def speed(self) -> float:
        try:
            return float(self._m.speed or 1.0)
        except Exception:
            return 1.0

    def set_speed(self, value: float) -> float:
        value = max(0.25, min(4.0, float(value)))
        self._m.speed = value
        return value

    # ── equalizer ─────────────────────────────────────────────────────
    # 10 fixed centre frequencies (Hz), low → high.
    EQ_FREQS = (31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000)

    def set_equalizer(self, gains: list[float]) -> None:
        """Apply a 10-band parametric EQ as a *labeled* mpv filter (@eq).

        Crucially this uses `af add/remove @eq` rather than assigning `af`
        wholesale — the loudness meter lives in the same chain as `@nav`, and
        replacing the chain would silently kill the visualizer. Empty or
        all-zero gains clear the EQ (removing @eq) and leave @nav intact.
        """
        try:
            self._m.command("af", "remove", "@eq")
        except Exception:
            pass  # not present yet — fine
        if not gains or all(abs(g) < 1e-6 for g in gains):
            return
        bands = []
        for freq, gain in zip(self.EQ_FREQS, gains[:10]):
            bands.append(f"equalizer=f={freq}:t=q:w=1.0:g={gain}")
        try:
            self._m.command("af", "add", f"@eq:lavfi=[{','.join(bands)}]")
        except Exception:
            pass  # mpv without the equalizer/lavfi filter — EQ silently off

    # ── output device selection ───────────────────────────────────────
    def get_audio_devices(self) -> list[dict]:
        """The output devices mpv can see, de-duplicated by description and
        with the noisy generic ALSA entries dropped (real hw:/plughw: kept).

        Enumerated on a short-lived throwaway mpv instance, never on the live
        player. Reading `audio-device-list` arms mpv's device-hotplug monitor
        for the whole life of that instance, and on macOS libmpv's CoreAudio
        hotplug callback can null-deref and segfault the entire process when a
        device later appears/disappears (seen on mpv 0.41). Probing on a
        disposable client we terminate right away keeps that monitor off the
        long-lived playback instance, so a headphone/Bluetooth change mid-
        session can't take the app down. Switching device (`set_audio_device`,
        by name) doesn't enumerate, so it stays on the live player.
        """
        raw: list[dict] = []
        probe = None
        try:
            probe = _mpv.MPV(video=False, terminal=False, idle=True)
            raw = probe.audio_device_list or []
        except Exception:
            raw = []
        finally:
            if probe is not None:
                try:
                    probe.terminate()
                except Exception:
                    pass
        keep_prefixes = ("auto", "pipewire", "pulse", "coreaudio", "wasapi", "alsa")
        seen: set[str] = set()
        out: list[dict] = []
        for dev in raw:
            name = dev.get("name", "")
            desc = dev.get("description", "")
            if name.startswith("alsa/") and not name.startswith(("alsa/hw:", "alsa/plughw:")):
                continue
            if not any(name.startswith(p) for p in keep_prefixes):
                continue
            if desc in seen:
                continue
            seen.add(desc)
            out.append(dev)
        return out or raw

    def get_current_audio_device(self) -> str:
        try:
            return self._m.audio_device or "auto"
        except Exception:
            return "auto"

    def set_audio_device(self, name: str) -> None:
        """Switch the output device. Normalizes the backend prefix to match the
        live driver, so a name captured under pipewire/… still resolves when
        mpv is actually running under pulse/… (common after a driver switch)."""
        try:
            ao = self._m.ao
            if isinstance(ao, list) and ao:
                ao = ao[0].get("name")
            if not isinstance(ao, str):
                ao = ""
            if ao and "/" in name:
                prefix, device_id = name.split("/", 1)
                if prefix != ao and ao in ("pulse", "pipewire", "alsa"):
                    name = f"{ao}/{device_id}"
            self._m.audio_device = name
        except Exception:
            pass

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
    speed = 1.0

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

    def set_speed(self, value: float) -> float:
        return value

    def set_equalizer(self, gains: list[float]) -> None:
        pass

    def get_audio_devices(self) -> list[dict]:
        return []

    def get_current_audio_device(self) -> str:
        return "auto"

    def set_audio_device(self, name: str) -> None:
        pass

    def terminate(self) -> None:
        pass


def create_player(
    on_position,
    on_track_end,
    ao: str | None = None,
    jukebox: bool = False,
    client=None,
    loop=None,
    on_unsupported=None,
    **opts,
):
    """Pick the playback engine.

    Default: the local libmpv `Player` (or `NullPlayer` if libmpv is missing).
    With `jukebox=True` and a live client+loop, return a `JukeboxPlayer` that
    drives the server's own audio out instead — same interface, no mpv. The
    app can flip between the two at runtime (see NaviTuiApp._make_player)."""
    if jukebox and client is not None and loop is not None:
        from navitui.jukebox import JukeboxPlayer

        return JukeboxPlayer(on_position, on_track_end, client, loop, on_unsupported)
    if not MPV_AVAILABLE:
        return NullPlayer()
    return Player(on_position, on_track_end, ao=ao, **opts)
