"""Optional desktop integrations: track-change notifications and Discord
rich presence. Everything degrades to a silent no-op — these must never be
a reason the player doesn't start.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

from navitui.models import Song


class Notifier:
    """Desktop notification on track change. Linux: notify-send (with the
    cached cover as the icon). macOS: osascript. Elsewhere: no-op."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self._tool = None
        if sys.platform.startswith("linux") and shutil.which("notify-send"):
            self._tool = "notify-send"
        elif sys.platform == "darwin" and shutil.which("osascript"):
            self._tool = "osascript"

    def toggle(self) -> bool:
        self.enabled = not self.enabled
        return self.enabled

    def track(self, song: Song, art_path: Path | None = None) -> None:
        if not self.enabled or self._tool is None:
            return
        body = f"{song.artist} · {song.album}" if song.album else song.artist
        try:
            if self._tool == "notify-send":
                cmd = [
                    "notify-send", "--app-name=NaviTui", "--expire-time=4000",
                    "--hint=string:x-canonical-private-synchronous:navitui",
                ]
                if art_path is not None:
                    cmd.append(f"--icon={art_path}")
                cmd += [song.title, body]
            else:
                script = (
                    f'display notification "{_esc(body)}" '
                    f'with title "NaviTui" subtitle "{_esc(song.title)}"'
                )
                cmd = ["osascript", "-e", script]
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


class DiscordPresence:
    """Rich presence via pypresence, entirely opt-in: needs the package,
    `discord_rich_presence = true`, an app id, and a running Discord."""

    def __init__(self, enabled: bool, app_id: str) -> None:
        self._rpc = None
        self._last = 0.0
        if not (enabled and app_id):
            return
        try:
            from pypresence import Presence

            rpc = Presence(app_id)
            rpc.connect()
            self._rpc = rpc
        except Exception:
            self._rpc = None

    def track(
        self,
        song: Song | None,
        playing: bool,
        position: float = 0.0,
        duration: float = 0.0,
    ) -> None:
        if self._rpc is None:
            return
        now = time.monotonic()
        if now - self._last < 3.0:  # discord rate limit headroom
            return
        self._last = now
        try:
            if song is None:
                self._rpc.clear()
                return
            fields: dict = {
                "details": song.title,
                "state": f"{song.artist}{' · paused' if not playing else ''}",
                "large_text": song.album or "NaviTui",
            }
            # a live progress bar: elapsed → total, only while actually playing
            if playing and duration > 0:
                start = int(time.time() - position)
                fields["start"] = start
                fields["end"] = start + int(duration)
            # only offer art/buttons when we have something Discord will accept
            # (asset key or public https url); local cache paths don't qualify.
            image = _presence_image(song)
            if image is not None:
                fields["large_image"] = image
            button = _presence_button(song)
            if button is not None:
                fields["buttons"] = [button]
            self._rpc.update(**fields)
        except Exception:
            self._rpc = None  # discord went away; stay quiet

    def stop(self) -> None:
        if self._rpc is not None:
            try:
                self._rpc.close()
            except Exception:
                pass
            self._rpc = None


def _presence_image(song: Song) -> str | None:
    """Discord's large_image wants an uploaded asset key or a public https
    URL. Cover art lives in a local cache the client can't reach, so unless
    a caller wires up a real url we have nothing valid to hand over."""
    url = getattr(song, "art_url", None)
    if isinstance(url, str) and url.startswith("https://"):
        return url
    return None


def _presence_button(song: Song) -> dict | None:
    """A share button needs a reachable https link; omit when absent rather
    than ship Discord a URL it will reject."""
    url = getattr(song, "share_url", None)
    if isinstance(url, str) and url.startswith("https://"):
        return {"label": "Listen", "url": url}
    return None
