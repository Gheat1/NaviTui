"""OS now-playing / media-key integration — one façade, per-platform backend.

- Linux   → MPRIS2 over D-Bus (`mpris.Mpris`)
- macOS   → MPNowPlayingInfoCenter / MPRemoteCommandCenter (`macos_media`)
- Windows → System Media Transport Controls (`windows_media`)
- anything else / missing deps → a silent no-op

Every backend implements the same four methods (`async start(controls)`,
`update(...)`, `set_position(...)`, `stop()`), so the app holds one object and
never branches on platform. Media integration is always optional: if a backend
can't activate, `start()` returns False and the rest no-ops — the player runs
exactly as before.

The mac/windows backends deliver media-key presses from a native run-loop or
message-pump thread, so the control callbacks they invoke must not touch the
Textual UI directly. We wrap each callback to hop back onto the app's asyncio
loop with `call_soon_threadsafe`; the Linux backend already runs on that loop,
so the wrap is a harmless one-tick deferral there.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Callable


class NullNowPlaying:
    """Used when no backend fits the platform or a dependency is missing."""

    async def start(self, controls: dict) -> bool:
        return False

    def update(self, *args, **kwargs) -> None:
        pass

    def set_position(self, position: float) -> None:
        pass

    def stop(self) -> None:
        pass


def _thread_safe(controls: dict, loop: asyncio.AbstractEventLoop) -> dict:
    """Return controls whose callbacks are safe to call from any thread: each
    is marshalled onto `loop`. Keys map to zero-arg callables except `seek`
    and `set_position`, which take one float — `*args` covers both."""

    def wrap(fn: Callable) -> Callable:
        def safe(*args) -> None:
            loop.call_soon_threadsafe(lambda: fn(*args))

        return safe

    return {name: wrap(fn) for name, fn in controls.items()}


def _make_backend():
    if sys.platform.startswith("linux"):
        from navitui.mpris import Mpris

        return Mpris()
    if sys.platform == "darwin":
        from navitui.macos_media import MacNowPlaying

        return MacNowPlaying()
    if sys.platform.startswith("win"):
        from navitui.windows_media import WindowsSMTC

        return WindowsSMTC()
    return NullNowPlaying()


class NowPlaying:
    """Platform-agnostic façade with the same shape the app used for MPRIS.

    Construction can't fail (a bad import falls back to the no-op backend), and
    every forwarded call is guarded so a misbehaving OS binding can never take
    the player down.
    """

    def __init__(self) -> None:
        try:
            self._backend = _make_backend()
        except Exception:
            self._backend = NullNowPlaying()

    async def start(self, controls: dict) -> bool:
        try:
            loop = asyncio.get_running_loop()
            return await self._backend.start(_thread_safe(controls, loop))
        except Exception:
            return False

    def update(self, *args, **kwargs) -> None:
        try:
            self._backend.update(*args, **kwargs)
        except Exception:
            pass

    def set_position(self, position: float) -> None:
        try:
            self._backend.set_position(position)
        except Exception:
            pass

    def stop(self) -> None:
        try:
            self._backend.stop()
        except Exception:
            pass


def create_nowplaying() -> NowPlaying:
    """Build the now-playing façade for the current platform."""
    return NowPlaying()
