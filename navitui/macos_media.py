"""macOS "now playing" — Control Center / media-key integration for NaviTui,
via PyObjC's MediaPlayer framework bindings.

This is the macOS sibling of `navitui.mpris` and exposes the exact same
façade (`start` / `update` / `set_position` / `stop`) so the app can pick a
backend per platform without caring which one it got.

Two halves, with very different confidence levels:

* DISPLAY (solid): pushing metadata to
  ``MPNowPlayingInfoCenter.defaultCenter()`` — title/artist/album, duration,
  elapsed time, playback rate, album art — plus ``playbackState``. Apple
  documents ``nowPlayingInfo`` as settable from any thread, so `update()`
  writes it directly from the app's asyncio thread. macOS extrapolates the
  progress bar from elapsed-time + rate on its own, so per-tick position
  pushes are unnecessary; `set_position()` only re-pushes when reality has
  drifted from that extrapolation (i.e. after a seek).

* CONTROLS: receiving media-key / Control Center commands back through
  ``MPRemoteCommandCenter.sharedCommandCenter()``. Command handlers are
  ordinary Python callables passed to ``addTargetWithHandler_`` (PyObjC bridges
  them to ObjC blocks). macOS delivers those blocks to the process's *main*
  dispatch queue, which is serviced only by the *main thread's* run loop — so
  everything here is done on the main thread: `start()` runs on the app's
  asyncio loop (which lives on the main thread), registers the commands and
  sets the shared NSApplication's activation policy to "accessory", then spins
  up an asyncio task (`_pump`) that runs the main run loop for a zero-length
  slice every few tens of milliseconds. That drains the main queue so the
  handlers actually fire — without a dedicated Cocoa thread the media-remote
  daemon tended to ignore (an earlier design registered and pumped on a
  background thread, so commands were never delivered and the app frequently
  didn't even register as the "now playing" client).

Fully optional: without PyObjC (or off macOS entirely) the module still
imports and `start()` returns False, making every method a no-op.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from navitui.models import Song

log = logging.getLogger(__name__)

# ── guarded native imports ─────────────────────────────────────────────
# PyObjC only ships macOS wheels, so on Linux/Windows these imports raise
# and the whole backend collapses to a no-op. Catch broad Exception, not
# just ImportError: a half-installed PyObjC can fail in stranger ways.
MACOS_MEDIA_AVAILABLE = True
try:
    import MediaPlayer  # pyobjc-framework-MediaPlayer
    from AppKit import (  # pyobjc-framework-Cocoa
        NSApplication,
        NSApplicationActivationPolicyAccessory,
        NSImage,
    )
    from Foundation import NSDate, NSDefaultRunLoopMode, NSRunLoop
except Exception:  # pragma: no cover - exercised only off-macOS
    MACOS_MEDIA_AVAILABLE = False

if MACOS_MEDIA_AVAILABLE:
    # Every constant is fetched defensively with its documented raw value as
    # the fallback, so a PyObjC release that misses one name can't take the
    # whole backend down at import time.
    _g = MediaPlayer.__dict__.get

    # MPNowPlayingPlaybackState (macOS 10.12.2+)
    _STATE_PLAYING = _g("MPNowPlayingPlaybackStatePlaying", 1)
    _STATE_PAUSED = _g("MPNowPlayingPlaybackStatePaused", 2)
    _STATE_STOPPED = _g("MPNowPlayingPlaybackStateStopped", 3)

    # MPRemoteCommandHandlerStatus
    _STATUS_SUCCESS = _g("MPRemoteCommandHandlerStatusSuccess", 0)
    _STATUS_FAILED = _g("MPRemoteCommandHandlerStatusCommandFailed", 200)

    # nowPlayingInfo dictionary keys
    _KEY_TITLE = _g("MPMediaItemPropertyTitle", "title")
    _KEY_ARTIST = _g("MPMediaItemPropertyArtist", "artist")
    _KEY_ALBUM = _g("MPMediaItemPropertyAlbumTitle", "albumTitle")
    _KEY_DURATION = _g("MPMediaItemPropertyPlaybackDuration", "playbackDuration")
    _KEY_ARTWORK = _g("MPMediaItemPropertyArtwork", "artwork")
    _KEY_ELAPSED = _g(
        "MPNowPlayingInfoPropertyElapsedPlaybackTime",
        "MPNowPlayingInfoPropertyElapsedPlaybackTime",
    )
    _KEY_RATE = _g(
        "MPNowPlayingInfoPropertyPlaybackRate",
        "MPNowPlayingInfoPropertyPlaybackRate",
    )
    _KEY_DEFAULT_RATE = _g(
        "MPNowPlayingInfoPropertyDefaultPlaybackRate",
        "MPNowPlayingInfoPropertyDefaultPlaybackRate",
    )
    _KEY_MEDIA_TYPE = _g(
        "MPNowPlayingInfoPropertyMediaType", "MPNowPlayingInfoPropertyMediaType"
    )
    _MEDIA_TYPE_AUDIO = _g("MPNowPlayingInfoMediaTypeAudio", 1)

# How far (seconds) the real position may drift from what macOS is already
# extrapolating before set_position() bothers to re-push the info dict.
_DRIFT_TOLERANCE = 2.0

# How often (seconds) the asyncio loop pumps the main run loop to drain the
# main dispatch queue where MPRemoteCommandCenter delivers its command blocks.
# This bounds media-key latency; small enough to feel instant, large enough to
# cost nothing.
_PUMP_INTERVAL = 0.05


class MacNowPlaying:
    """App-facing façade: ``await start(controls)``, then ``update(...)`` on
    every track/pause change. All methods are safe no-ops when unavailable."""

    def __init__(self) -> None:
        self._active = False
        self._controls: dict[str, Callable[..., None]] = {}
        # asyncio task that pumps the main run loop (see _pump).
        self._pump_task: "asyncio.Task[None] | None" = None
        # (command, target-token) pairs so stop() can removeTarget_ cleanly.
        # Registration, pumping and teardown all run on the main thread now, so
        # no lock is needed to guard this.
        self._targets: list[tuple[Any, Any]] = []
        # Cached nowPlayingInfo (a plain Python dict — PyObjC bridges it on
        # every setNowPlayingInfo_ call) plus what we last told macOS, so
        # set_position() can detect drift without any native calls.
        self._info: dict[Any, Any] | None = None
        self._pushed_pos = 0.0
        self._pushed_at = 0.0
        self._rate = 0.0
        # Artwork is cached per path: NSImage decode + MPMediaItemArtwork
        # wrapping happen once per cover, not once per update.
        self._art_path: str | None = None
        self._artwork: Any = None

    # ── lifecycle ──────────────────────────────────────────────────────
    async def start(self, controls: dict) -> bool:
        """Register remote commands on the main thread and start pumping the
        main run loop from the asyncio loop.

        This runs on the app's asyncio loop, which lives on the *main* thread —
        and that matters: macOS delivers MPRemoteCommandCenter handler blocks to
        the main dispatch queue, which only the main thread's run loop drains.
        The earlier design registered and pumped on a background thread, so the
        commands were never delivered (and the process often wasn't accepted as
        the "now playing" app at all). Doing it here, on the main thread, is the
        supported path.

        Returns True when the native side came up, False to no-op (missing
        PyObjC, not macOS, or command registration blew up).
        """
        if not MACOS_MEDIA_AVAILABLE or sys.platform != "darwin":
            return False
        self._controls = controls
        try:
            # Register with the window server as a UI-less accessory app;
            # without *some* activation policy the media-remote daemon may
            # never consider this bare terminal process a "now playing" app.
            # Must happen on the main thread — which is where we are.
            NSApplication.sharedApplication().setActivationPolicy_(
                NSApplicationActivationPolicyAccessory
            )
            self._register_commands()
        except Exception:
            log.debug("macos_media: command registration failed", exc_info=True)
            self._unregister_commands()
            return False
        self._active = True
        self._pump_task = asyncio.ensure_future(self._pump())
        return True

    def stop(self) -> None:
        """Tear down: stop pumping, clear now-playing info, drop command
        targets. Idempotent and exception-proof."""
        self._active = False
        if self._pump_task is not None:
            self._pump_task.cancel()
            self._pump_task = None
        self._info = None
        try:
            if MACOS_MEDIA_AVAILABLE:
                center = MediaPlayer.MPNowPlayingInfoCenter.defaultCenter()
                center.setNowPlayingInfo_(None)
                center.setPlaybackState_(_STATE_STOPPED)
        except Exception:
            log.debug("macos_media: clearing now-playing info failed", exc_info=True)
        self._unregister_commands()

    # ── main run-loop pump ─────────────────────────────────────────────
    async def _pump(self) -> None:
        """Cooperatively drain the main run loop from the asyncio loop.

        MPRemoteCommandCenter blocks are dispatched to the main queue; running
        the main run loop for a zero-length slice services them (and any pending
        Cocoa sources) and returns immediately, so this never blocks the app.
        Runs on the main thread because that is where the asyncio loop lives."""
        run_loop = NSRunLoop.currentRunLoop()
        try:
            while self._active:
                try:
                    run_loop.runMode_beforeDate_(
                        NSDefaultRunLoopMode,
                        NSDate.dateWithTimeIntervalSinceNow_(0),
                    )
                except Exception:
                    log.debug("macos_media: run loop pump failed", exc_info=True)
                await asyncio.sleep(_PUMP_INTERVAL)
        except asyncio.CancelledError:
            pass
        finally:
            self._unregister_commands()

    def _register_commands(self) -> None:
        """Enable transport commands and point them at the app callbacks.
        Runs on the run-loop thread. The callbacks are already thread-safe,
        so handlers may invoke them straight from wherever macOS calls in."""
        center = MediaPlayer.MPRemoteCommandCenter.sharedCommandCenter()
        simple = (
            (center.togglePlayPauseCommand(), "play_pause"),
            (center.playCommand(), "play"),
            (center.pauseCommand(), "pause"),
            (center.stopCommand(), "stop"),
            (center.nextTrackCommand(), "next"),
            (center.previousTrackCommand(), "prev"),
        )
        for cmd, name in simple:
            cmd.setEnabled_(True)
            cmd.removeTarget_(None)
            token = cmd.addTargetWithHandler_(self._make_handler(name))
            self._targets.append((cmd, token))

        # Control Center scrubber → absolute seek.
        pos_cmd = center.changePlaybackPositionCommand()
        pos_cmd.setEnabled_(True)
        pos_cmd.removeTarget_(None)
        token = pos_cmd.addTargetWithHandler_(self._handle_change_position)
        self._targets.append((pos_cmd, token))

        # Explicitly disabled: enabling skip/seek variants makes the
        # Control Center UI trade its prev/next buttons for skip buttons,
        # and NaviTui has no rate control. Relative "seek" stays a
        # keyboard-only affair inside the app.
        for cmd in (
            center.changePlaybackRateCommand(),
            center.seekForwardCommand(),
            center.seekBackwardCommand(),
            center.skipForwardCommand(),
            center.skipBackwardCommand(),
        ):
            try:
                cmd.setEnabled_(False)
            except Exception:
                pass

    def _unregister_commands(self) -> None:
        """Detach every handler we added and disable the commands. Safe to
        call twice."""
        targets, self._targets = self._targets, []
        for cmd, token in targets:
            try:
                cmd.removeTarget_(token)
                cmd.setEnabled_(False)
            except Exception:
                log.debug("macos_media: removeTarget failed", exc_info=True)

    def _make_handler(self, name: str) -> Callable[[Any], int]:
        """Build a zero-arg-control handler. PyObjC turns the plain Python
        callable into the ObjC block addTargetWithHandler_ expects."""

        def handler(event: Any) -> int:
            try:
                control = self._controls.get(name)
                if control is None:
                    return _STATUS_FAILED
                control()
                return _STATUS_SUCCESS
            except Exception:
                log.debug("macos_media: %s handler failed", name, exc_info=True)
                return _STATUS_FAILED

        return handler

    def _handle_change_position(self, event: Any) -> int:
        """changePlaybackPositionCommand → controls["set_position"]."""
        try:
            control = self._controls.get("set_position")
            if control is None:
                return _STATUS_FAILED
            control(float(event.positionTime()))
            return _STATUS_SUCCESS
        except Exception:
            log.debug("macos_media: position handler failed", exc_info=True)
            return _STATUS_FAILED

    # ── display half ───────────────────────────────────────────────────
    def update(
        self,
        song: "Song | None",
        playing: bool,
        position: float,
        volume: int,
        art_path: str | None = None,
    ) -> None:
        """Push metadata + playback state to MPNowPlayingInfoCenter.

        Called from the app's asyncio thread; Apple documents nowPlayingInfo
        as settable from any thread, so no marshalling to the run-loop
        thread is needed. `volume` is accepted for interface parity only —
        the now-playing dictionary has no volume key on macOS.
        """
        if not self._active:
            return
        try:
            center = MediaPlayer.MPNowPlayingInfoCenter.defaultCenter()
            if song is None:
                self._info = None
                self._rate = 0.0
                center.setNowPlayingInfo_(None)
                center.setPlaybackState_(_STATE_STOPPED)
                return
            rate = 1.0 if playing else 0.0
            info: dict[Any, Any] = {
                _KEY_MEDIA_TYPE: _MEDIA_TYPE_AUDIO,
                # Coerce every field: a None sneaking into the bridged
                # dictionary would throw deep inside PyObjC/XPC.
                _KEY_TITLE: song.title or "",
                _KEY_ARTIST: song.artist or "",
                _KEY_ALBUM: song.album or "",
                _KEY_DURATION: float(song.duration or 0),
                _KEY_ELAPSED: float(position),
                _KEY_RATE: rate,
                _KEY_DEFAULT_RATE: 1.0,
            }
            artwork = self._artwork_for(art_path)
            if artwork is not None:
                info[_KEY_ARTWORK] = artwork
            center.setNowPlayingInfo_(info)
            center.setPlaybackState_(_STATE_PLAYING if playing else _STATE_PAUSED)
            self._info = info
            self._rate = rate
            self._pushed_pos = float(position)
            self._pushed_at = time.monotonic()
        except Exception:
            log.debug("macos_media: update failed", exc_info=True)

    def set_position(self, position: float) -> None:
        """Cheap per-tick position update. macOS extrapolates elapsed time
        from the last (elapsed, rate) pair itself, so this only re-pushes
        when the real position has drifted — i.e. after an in-app seek."""
        if not self._active or self._info is None:
            return
        expected = self._pushed_pos + (time.monotonic() - self._pushed_at) * self._rate
        if abs(position - expected) <= _DRIFT_TOLERANCE:
            return
        try:
            self._info[_KEY_ELAPSED] = float(position)
            MediaPlayer.MPNowPlayingInfoCenter.defaultCenter().setNowPlayingInfo_(
                self._info
            )
            self._pushed_pos = float(position)
            self._pushed_at = time.monotonic()
        except Exception:
            log.debug("macos_media: set_position failed", exc_info=True)

    # ── artwork ────────────────────────────────────────────────────────
    def _artwork_for(self, art_path: str | None) -> Any:
        """MPMediaItemArtwork for a cover file, cached per path. The request
        handler hands back the original NSImage for any requested size —
        no drawing, so it is safe on whatever thread macOS calls it from."""
        if art_path == self._art_path:
            return self._artwork
        self._art_path = art_path
        self._artwork = None
        if not art_path:
            return None
        try:
            image = NSImage.alloc().initWithContentsOfFile_(art_path)
            if image is None:  # unreadable / unsupported file
                return None

            def request(size: Any) -> Any:  # size: CGSize, unused
                return image

            self._artwork = (
                MediaPlayer.MPMediaItemArtwork.alloc().initWithBoundsSize_requestHandler_(
                    image.size(), request
                )
            )
        except Exception:
            log.debug("macos_media: artwork load failed", exc_info=True)
            self._artwork = None
        return self._artwork
