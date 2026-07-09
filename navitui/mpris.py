"""MPRIS2 — desktop media controls (media keys, playerctl, GNOME/KDE/waybar
widgets) for NaviTui, via dbus-fast.

dbus-fast is asyncio-native, so the service lives on the app's own event
loop: no threads, and incoming controls (PlayPause from a media key) call
straight back into app actions. Fully optional — without dbus-fast, or off
the session bus (macOS/Windows), `Mpris.start()` quietly does nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

MPRIS_AVAILABLE = True
try:
    from dbus_fast.aio import MessageBus
    from dbus_fast.constants import PropertyAccess
    from dbus_fast.service import ServiceInterface, dbus_property, method
except ImportError:
    MPRIS_AVAILABLE = False

    class ServiceInterface:  # type: ignore[no-redef]
        def __init__(self, *a) -> None: ...

    def method(*a, **kw):  # type: ignore[no-redef]
        return lambda f: f

    def dbus_property(*a, **kw):  # type: ignore[no-redef]
        return lambda f: f

    class PropertyAccess:  # type: ignore[no-redef]
        READ = None

if TYPE_CHECKING:
    from navitui.models import Song

BUS_NAME = "org.mpris.MediaPlayer2.navitui"
OBJECT_PATH = "/org/mpris/MediaPlayer2"


class _Root(ServiceInterface):
    def __init__(self) -> None:
        super().__init__("org.mpris.MediaPlayer2")

    @method()
    def Raise(self):
        pass

    @method()
    def Quit(self):
        pass

    @dbus_property(access=PropertyAccess.READ)
    def Identity(self) -> "s":  # noqa: F821
        return "NaviTui"

    @dbus_property(access=PropertyAccess.READ)
    def CanQuit(self) -> "b":  # noqa: F821
        return False

    @dbus_property(access=PropertyAccess.READ)
    def CanRaise(self) -> "b":  # noqa: F821
        return False

    @dbus_property(access=PropertyAccess.READ)
    def HasTrackList(self) -> "b":  # noqa: F821
        return False

    @dbus_property(access=PropertyAccess.READ)
    def SupportedUriSchemes(self) -> "as":  # noqa: F821
        return []

    @dbus_property(access=PropertyAccess.READ)
    def SupportedMimeTypes(self) -> "as":  # noqa: F821
        return []


class _Player(ServiceInterface):
    """The Player interface. Transport methods call back into the app."""

    def __init__(self, controls: dict[str, Callable[[], None]]) -> None:
        super().__init__("org.mpris.MediaPlayer2.Player")
        self._controls = controls
        self.status = "Stopped"
        self.metadata: dict = {}
        self.volume = 1.0
        self.position_us = 0

    # ── transport (media keys land here) ──────────────────────────────
    @method()
    def PlayPause(self):
        self._controls["play_pause"]()

    @method()
    def Play(self):
        self._controls["play"]()

    @method()
    def Pause(self):
        self._controls["pause"]()

    @method()
    def Stop(self):
        self._controls["stop"]()

    @method()
    def Next(self):
        self._controls["next"]()

    @method()
    def Previous(self):
        self._controls["prev"]()

    @method()
    def Seek(self, offset: "x"):  # noqa: F821
        self._controls["seek"](offset / 1_000_000)

    @method()
    def SetPosition(self, track_id: "o", position: "x"):  # noqa: F821
        self._controls["set_position"](position / 1_000_000)

    @method()
    def OpenUri(self, uri: "s"):  # noqa: F821
        pass

    # ── state ─────────────────────────────────────────────────────────
    @dbus_property(access=PropertyAccess.READ)
    def PlaybackStatus(self) -> "s":  # noqa: F821
        return self.status

    @dbus_property(access=PropertyAccess.READ)
    def Metadata(self) -> "a{sv}":  # noqa: F821
        return self.metadata

    @dbus_property(access=PropertyAccess.READ)
    def Position(self) -> "x":  # noqa: F821
        return self.position_us

    @dbus_property(access=PropertyAccess.READ)
    def Volume(self) -> "d":  # noqa: F821
        return self.volume

    @dbus_property(access=PropertyAccess.READ)
    def Rate(self) -> "d":  # noqa: F821
        return 1.0

    @dbus_property(access=PropertyAccess.READ)
    def MinimumRate(self) -> "d":  # noqa: F821
        return 1.0

    @dbus_property(access=PropertyAccess.READ)
    def MaximumRate(self) -> "d":  # noqa: F821
        return 1.0

    @dbus_property(access=PropertyAccess.READ)
    def CanGoNext(self) -> "b":  # noqa: F821
        return True

    @dbus_property(access=PropertyAccess.READ)
    def CanGoPrevious(self) -> "b":  # noqa: F821
        return True

    @dbus_property(access=PropertyAccess.READ)
    def CanPlay(self) -> "b":  # noqa: F821
        return True

    @dbus_property(access=PropertyAccess.READ)
    def CanPause(self) -> "b":  # noqa: F821
        return True

    @dbus_property(access=PropertyAccess.READ)
    def CanSeek(self) -> "b":  # noqa: F821
        return True

    @dbus_property(access=PropertyAccess.READ)
    def CanControl(self) -> "b":  # noqa: F821
        return True


class Mpris:
    """App-facing façade: `await start(controls)`, then `update(...)` on
    every track/pause change. All methods are safe no-ops when unavailable."""

    def __init__(self) -> None:
        self._bus = None
        self._player: _Player | None = None

    async def start(self, controls: dict[str, Callable[[], None]]) -> bool:
        if not MPRIS_AVAILABLE:
            return False
        try:
            from dbus_fast import Variant  # noqa: F401  (import check)

            self._bus = await MessageBus().connect()
            self._player = _Player(controls)
            self._bus.export(OBJECT_PATH, _Root())
            self._bus.export(OBJECT_PATH, self._player)
            await self._bus.request_name(BUS_NAME)
            return True
        except Exception:
            self._bus = None
            self._player = None
            return False

    def update(
        self,
        song: "Song | None",
        playing: bool,
        position: float,
        volume: int,
        art_path: str | None = None,
    ) -> None:
        if self._player is None:
            return
        from dbus_fast import Variant

        player = self._player
        player.status = "Playing" if playing else ("Paused" if song else "Stopped")
        player.position_us = int(position * 1_000_000)
        player.volume = max(0.0, volume / 100)
        if song is None:
            player.metadata = {}
        else:
            meta = {
                # dbus can't serialize None — coerce every string/number field
                # (a track with no album/title/duration would otherwise raise
                # here and abort the whole _announce fan-out, killing
                # notifications + discord for that track change)
                "mpris:trackid": Variant("o", f"/dev/navitui/track/{abs(hash(song.id))}"),
                "mpris:length": Variant("x", int((song.duration or 0) * 1_000_000)),
                "xesam:title": Variant("s", song.title or ""),
                "xesam:artist": Variant("as", [song.artist] if song.artist else []),
                "xesam:album": Variant("s", song.album or ""),
            }
            if art_path:
                meta["mpris:artUrl"] = Variant("s", f"file://{art_path}")
            player.metadata = meta
        try:
            player.emit_properties_changed(
                {
                    "PlaybackStatus": player.status,
                    "Metadata": player.metadata,
                    "Volume": player.volume,
                }
            )
        except Exception:
            pass

    def set_position(self, position: float) -> None:
        """Cheap per-tick position update — no signal, per the MPRIS spec."""
        if self._player is not None:
            self._player.position_us = int(position * 1_000_000)

    def stop(self) -> None:
        if self._bus is not None:
            try:
                self._bus.disconnect()
            except Exception:
                pass
            self._bus = None
            self._player = None
