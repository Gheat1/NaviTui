"""Windows SMTC — System Media Transport Controls (the flyout that pops up
next to the volume OSD, plus keyboard media keys) for NaviTui, via PyWinRT.

Mirrors `navitui.mpris.Mpris` exactly (`await start(controls)` /
`update(...)` / `set_position(...)` / `stop()`) so a dispatcher can pick a
backend per platform. Fully optional — on Linux/macOS, or on Windows without
the `winrt-*` packages installed, `WindowsSMTC.start()` quietly returns
False and every other method is a no-op.

How the SMTC instance is obtained (in order):

1. Interop route (preferred): `kernel32.GetConsoleWindow()` gives the HWND
   of the console hosting the TUI, and `winrt.windows.media.interop
   .get_for_window(hwnd)` wraps `ISystemMediaTransportControlsInterop::
   GetForWindow` to bind an SMTC to that window. This is the documented way
   for a desktop (non-UWP) process to own an SMTC directly.
2. MediaPlayer route (fallback): a dummy `Windows.Media.Playback
   .MediaPlayer` with its `command_manager` disabled exposes a real SMTC we
   can drive by hand. Used when there is no console window (e.g. some GPU
   terminals detach from conhost) or when the interop package is missing.
   The legacy monolithic `winsdk` package only supports this route (it never
   shipped a media interop module).

What is known-solid vs what needs a real Windows box:

* Solid: all names below were checked against the pywinrt 3.x projection
  stubs (`winrt-Windows.Media` et al.) — enum members, snake_case
  properties, `get_for_window(hwnd: int)`, timeline properties as
  `datetime.timedelta`, awaitable `IAsyncOperation`.
* Needs on-target testing: whether Windows *delivers* ButtonPressed events
  to an SMTC bound to a console HWND. WinRT dispatches these on an MTA
  thread-pool thread (pywinrt initializes an MTA), so no message pump of
  ours should be required — but terminal hosts vary (conhost vs Windows
  Terminal's pseudo-console), and this is the fragile part. If events go
  missing under Windows Terminal, the MediaPlayer fallback route is the
  known-good escape hatch.

Threading model: `start()`/`update()`/`set_position()` are called on the
app's asyncio thread. Button/seek callbacks arrive on WinRT thread-pool
threads; the `controls` callables are documented thread-safe, so handlers
invoke them directly. Cover-art loading needs an async WinRT call
(`StorageFile.get_file_from_path_async`), so `update()` pushes text
metadata synchronously and schedules the thumbnail onto the loop captured
in `start()`, with a sequence counter so a slow art load can never clobber
a newer track's display.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import sys
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from navitui.models import Song

log = logging.getLogger(__name__)

# ── guarded imports ────────────────────────────────────────────────────────
# Nothing WinRT-related may import unguarded: this module must load cleanly
# on every OS. Modern modular pywinrt (`winrt-*` packages, `winrt.*`
# namespace) is tried first, then the deprecated monolithic `winsdk`.
# `except Exception` (not just ImportError): a broken DLL load on Windows
# must also degrade to "unavailable", never crash the app at import time.

SMTC_AVAILABLE = False
_get_for_window: Callable[[int], Any] | None = None  # interop route
_MediaPlayer: Any = None  # fallback route

if sys.platform == "win32":
    try:
        from winrt.windows.media import (  # type: ignore[import-not-found]
            MediaPlaybackStatus,
            MediaPlaybackType,
            SystemMediaTransportControlsButton,
            SystemMediaTransportControlsTimelineProperties,
        )
        from winrt.windows.storage import StorageFile  # type: ignore[import-not-found]
        from winrt.windows.storage.streams import (  # type: ignore[import-not-found]
            RandomAccessStreamReference,
        )

        SMTC_AVAILABLE = True
    except Exception:
        try:
            from winsdk.windows.media import (  # type: ignore[import-not-found]
                MediaPlaybackStatus,
                MediaPlaybackType,
                SystemMediaTransportControlsButton,
                SystemMediaTransportControlsTimelineProperties,
            )
            from winsdk.windows.storage import StorageFile  # type: ignore[import-not-found]
            from winsdk.windows.storage.streams import (  # type: ignore[import-not-found]
                RandomAccessStreamReference,
            )

            SMTC_AVAILABLE = True
        except Exception:
            pass

if SMTC_AVAILABLE:
    # Interop module only exists in modular pywinrt (winrt-Windows.Media.Interop);
    # its import chain pulls winrt.windows.media, so it can never mismatch a
    # winsdk-provided base — if the base came from winsdk this simply fails.
    try:
        from winrt.windows.media.interop import (  # type: ignore[import-not-found]
            get_for_window as _get_for_window,
        )
    except Exception:
        _get_for_window = None
    try:
        from winrt.windows.media.playback import (  # type: ignore[import-not-found]
            MediaPlayer as _MediaPlayer,
        )
    except Exception:
        try:
            from winsdk.windows.media.playback import (  # type: ignore[import-not-found]
                MediaPlayer as _MediaPlayer,
            )
        except Exception:
            _MediaPlayer = None

# Relative jump for the SMTC fast-forward / rewind buttons, in seconds
# (SMTC has no native relative-seek command, so we synthesize one).
_SEEK_STEP = 10.0


def _console_hwnd() -> int:
    """HWND of the console hosting this process, or 0 if there is none
    (windowed terminal that detached conhost, pythonw, service, ...)."""
    try:
        return int(ctypes.windll.kernel32.GetConsoleWindow())  # type: ignore[attr-defined]
    except Exception:
        return 0


class WindowsSMTC:
    """App-facing façade, interface-identical to `mpris.Mpris`: `await
    start(controls)`, then `update(...)` on every track/pause change. All
    methods are safe no-ops when unavailable, and no native exception ever
    escapes to the app."""

    def __init__(self) -> None:
        self._smtc: Any = None
        self._player: Any = None  # keepalive for the MediaPlayer fallback
        self._controls: dict[str, Callable] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        # (remove_method, token) pairs so stop() can detach every handler
        self._tokens: list[tuple[Callable, Any]] = []
        self._duration: float = 0.0  # last known track length, for timelines
        self._seq = 0  # update generation, guards stale async art loads
        self._art_cache: tuple[str, Any] | None = None  # (path, stream ref)

    # ── lifecycle ──────────────────────────────────────────────────────
    async def start(self, controls: dict[str, Callable]) -> bool:
        """Acquire an SMTC, enable transport buttons, wire handlers.
        Returns False (and stays inert) if anything is missing."""
        if not SMTC_AVAILABLE:
            return False
        self._controls = controls
        self._loop = asyncio.get_running_loop()
        try:
            smtc = self._acquire_smtc()
            if smtc is None:
                return False

            # Advertise the transport surface. Volume is intentionally not
            # exposed: SMTC has no volume concept (Windows handles it).
            smtc.is_enabled = True
            smtc.is_play_enabled = True
            smtc.is_pause_enabled = True
            smtc.is_stop_enabled = True
            smtc.is_next_enabled = True
            smtc.is_previous_enabled = True
            smtc.is_fast_forward_enabled = True  # -> relative seek
            smtc.is_rewind_enabled = True

            # Handlers fire on a WinRT thread-pool thread; the controls
            # callables are thread-safe by contract, so call them directly.
            self._tokens.append(
                (smtc.remove_button_pressed, smtc.add_button_pressed(self._on_button))
            )
            # Dragging the SMTC seek bar (needs timeline props to be pushed,
            # which update()/set_position() do).
            self._tokens.append(
                (
                    smtc.remove_playback_position_change_requested,
                    smtc.add_playback_position_change_requested(self._on_position),
                )
            )

            smtc.playback_status = MediaPlaybackStatus.STOPPED
            self._smtc = smtc
            return True
        except Exception:
            log.debug("SMTC start failed", exc_info=True)
            self.stop()
            return False

    def _acquire_smtc(self) -> Any:
        """Try the console-HWND interop first, then the MediaPlayer shim."""
        if _get_for_window is not None:
            hwnd = _console_hwnd()
            if hwnd:
                try:
                    return _get_for_window(hwnd)
                except Exception:
                    log.debug("SMTC GetForWindow failed", exc_info=True)
        if _MediaPlayer is not None:
            try:
                player = _MediaPlayer()
                # With the command manager on, MediaPlayer would consume the
                # SMTC buttons itself; disabled, ButtonPressed reaches us and
                # we own the display updater outright.
                player.command_manager.is_enabled = False
                self._player = player  # must outlive the SMTC
                return player.system_media_transport_controls
            except Exception:
                log.debug("SMTC MediaPlayer fallback failed", exc_info=True)
                self._player = None
        return None

    def stop(self) -> None:
        """Tear down: detach handlers, mark Closed, release the shim player."""
        smtc, self._smtc = self._smtc, None
        self._seq += 1  # cancel any in-flight thumbnail task
        if smtc is not None:
            for remove, token in self._tokens:
                try:
                    remove(token)
                except Exception:
                    pass
            try:
                smtc.display_updater.clear_all()
                smtc.display_updater.update()
            except Exception:
                pass
            try:
                smtc.playback_status = MediaPlaybackStatus.CLOSED
                smtc.is_enabled = False
            except Exception:
                pass
        self._tokens.clear()
        player, self._player = self._player, None
        if player is not None:
            try:
                player.close()  # IClosable
            except Exception:
                pass
        self._art_cache = None

    # ── state pushed from the app ──────────────────────────────────────
    def update(
        self,
        song: "Song | None",
        playing: bool,
        position: float,
        volume: int,  # unused: SMTC has no volume surface (kept for parity)
        art_path: str | None = None,
    ) -> None:
        smtc = self._smtc
        if smtc is None:
            return
        self._seq += 1
        try:
            smtc.playback_status = (
                MediaPlaybackStatus.PLAYING
                if playing
                else (MediaPlaybackStatus.PAUSED if song else MediaPlaybackStatus.STOPPED)
            )
            updater = smtc.display_updater
            if song is None:
                self._duration = 0.0
                updater.clear_all()
                updater.update()
                return

            self._duration = float(song.duration or 0)
            updater.type = MediaPlaybackType.MUSIC
            props = updater.music_properties
            # WinRT strings reject None — coerce, same rationale as mpris.py
            props.title = song.title or ""
            props.artist = song.artist or ""
            props.album_title = song.album or ""

            if art_path and self._art_cache and self._art_cache[0] == art_path:
                # Same cover as last push (pause/resume, next track on the
                # same album) — reuse the stream reference synchronously.
                updater.thumbnail = self._art_cache[1]
                updater.update()
            else:
                # Push text now; the thumbnail needs an async StorageFile
                # round-trip, after which the display is updated again.
                updater.update()
                if art_path and self._loop is not None:
                    # run_coroutine_threadsafe is safe from any thread,
                    # including the loop's own.
                    asyncio.run_coroutine_threadsafe(
                        self._load_thumbnail(art_path, self._seq), self._loop
                    )

            self._push_timeline(position)
        except Exception:
            log.debug("SMTC update failed", exc_info=True)

    def set_position(self, position: float) -> None:
        """Per-tick timeline refresh (keeps the SMTC seek bar honest)."""
        if self._smtc is None:
            return
        try:
            self._push_timeline(position)
        except Exception:
            log.debug("SMTC set_position failed", exc_info=True)

    def _push_timeline(self, position: float) -> None:
        """Windows requires start <= min_seek <= position <= max_seek <= end."""
        duration = max(self._duration, 0.0)
        position = max(0.0, min(position, duration) if duration else position)
        tl = SystemMediaTransportControlsTimelineProperties()
        tl.start_time = timedelta(0)
        tl.min_seek_time = timedelta(0)
        tl.position = timedelta(seconds=position)
        tl.max_seek_time = timedelta(seconds=duration)
        tl.end_time = timedelta(seconds=duration)
        self._smtc.update_timeline_properties(tl)

    async def _load_thumbnail(self, art_path: str, seq: int) -> None:
        """Resolve a filesystem cover into a RandomAccessStreamReference and
        re-publish the display. Runs on the app loop; pywinrt async ops are
        directly awaitable there. Requires an absolute path."""
        try:
            file = await StorageFile.get_file_from_path_async(art_path)
            ref = RandomAccessStreamReference.create_from_file(file)
        except Exception:
            log.debug("SMTC thumbnail load failed for %s", art_path, exc_info=True)
            return
        if seq != self._seq or self._smtc is None:
            return  # a newer update() or stop() superseded this load
        try:
            self._art_cache = (art_path, ref)
            updater = self._smtc.display_updater
            updater.thumbnail = ref
            updater.update()
        except Exception:
            log.debug("SMTC thumbnail push failed", exc_info=True)

    # ── events from Windows (WinRT thread-pool threads) ────────────────
    def _on_button(self, sender: Any, args: Any) -> None:
        """Media key / SMTC flyout button. Never let an exception escape —
        it would surface inside the WinRT callback machinery."""
        try:
            button = args.button
            b = SystemMediaTransportControlsButton
            if button == b.FAST_FORWARD:
                self._controls["seek"](_SEEK_STEP)
            elif button == b.REWIND:
                self._controls["seek"](-_SEEK_STEP)
            else:
                name = {
                    b.PLAY: "play",
                    b.PAUSE: "pause",
                    b.STOP: "stop",
                    b.NEXT: "next",
                    b.PREVIOUS: "prev",
                }.get(button)
                if name is not None:
                    self._controls[name]()
        except Exception:
            log.debug("SMTC button handler failed", exc_info=True)

    def _on_position(self, sender: Any, args: Any) -> None:
        """User dragged the SMTC seek bar — absolute seek."""
        try:
            self._controls["set_position"](
                args.requested_playback_position.total_seconds()
            )
        except Exception:
            log.debug("SMTC position handler failed", exc_info=True)
