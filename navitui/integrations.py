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
from typing import Callable

from navitui.models import Song

# org.freedesktop.Notifications — the FreeDesktop notifications service.
# Preferred on linux so we can attach action buttons and hear back which one
# the user clicked; everything degrades to notify-send/osascript below.
NOTIFY_SERVICE = "org.freedesktop.Notifications"
NOTIFY_PATH = "/org/freedesktop/Notifications"

# action buttons, in display order: (id, label). the id round-trips through
# the ActionInvoked signal and maps to a control callback (see Notifier.start).
NOTIFY_ACTIONS: list[tuple[str, str]] = [
    ("previous", "⏮ Prev"),
    ("play-pause", "⏯ Play/Pause"),
    ("next", "⏭ Next"),
]


class Notifier:
    """Desktop notification on track change.

    Preferred (linux, session bus reachable): the org.freedesktop.Notifications
    dbus interface via dbus-fast — asyncio-native like MPRIS, so we can attach
    action buttons and handle the ActionInvoked signal straight on the app's
    event loop. Fallback: notify-send (linux) / osascript (macOS), no buttons.
    Elsewhere: no-op. Notifications never block and never stop playback."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self._tool = None
        if sys.platform.startswith("linux") and shutil.which("notify-send"):
            self._tool = "notify-send"
        elif sys.platform == "darwin" and shutil.which("osascript"):
            self._tool = "osascript"
        # dbus backend, populated by start(); None means "use _tool"
        self._bus = None
        self._controls: dict[str, Callable[[], None]] = {}
        self._last_id = 0
        self._tasks: set = set()  # hold refs to in-flight Notify sends

    def toggle(self) -> bool:
        self.enabled = not self.enabled
        return self.enabled

    async def start(self, controls: dict[str, Callable[[], None]]) -> bool:
        """Connect to org.freedesktop.Notifications and route ActionInvoked to
        the given control callbacks (keys: previous/play-pause/next). Returns
        True when the dbus backend is live; on any failure we keep _tool.

        Runs on the app's own event loop, so an invoked action calls the
        control directly — no threads, exactly like MPRIS media keys."""
        if not sys.platform.startswith("linux"):
            return False
        try:
            from dbus_fast import Message, MessageType
            from dbus_fast.aio import MessageBus
        except ImportError:
            return False
        try:
            bus = await MessageBus().connect()
            # only claim dbus if the service actually answers, else fall back
            reply = await bus.call(
                Message(
                    destination=NOTIFY_SERVICE,
                    path=NOTIFY_PATH,
                    interface=NOTIFY_SERVICE,
                    member="GetCapabilities",
                )
            )
            if reply is None or reply.message_type != MessageType.METHOD_RETURN:
                bus.disconnect()
                return False
            # subscribe to ActionInvoked for our own notification ids
            await bus.call(
                Message(
                    destination="org.freedesktop.DBus",
                    path="/org/freedesktop/DBus",
                    interface="org.freedesktop.DBus",
                    member="AddMatch",
                    signature="s",
                    body=[
                        f"type='signal',interface='{NOTIFY_SERVICE}',"
                        "member='ActionInvoked'"
                    ],
                )
            )
            bus.add_message_handler(self._on_message)
            self._bus = bus
            self._controls = controls
            return True
        except Exception:
            self._bus = None
            return False

    def _on_message(self, msg) -> None:
        """dbus signal handler (on the event loop). ActionInvoked carries the
        notification id we sent and the invoked action id; dispatch it to the
        matching control if it targets our current notification."""
        if (
            msg.member != "ActionInvoked"
            or msg.interface != NOTIFY_SERVICE
            or not msg.body
        ):
            return
        notif_id = msg.body[0]
        action = msg.body[1] if len(msg.body) > 1 else ""
        if notif_id != self._last_id:
            return
        cb = self._controls.get(action)
        if cb is not None:
            try:
                cb()
            except Exception:
                pass

    def track(self, song: Song, art_path: Path | None = None) -> None:
        if not self.enabled:
            return
        if self._bus is not None:
            self._notify_dbus(song, art_path)
            return
        if self._tool is None:
            return
        self._notify_tool(song, art_path)

    def _notify_dbus(self, song: Song, art_path: Path | None) -> None:
        """Fire-and-forget Notify with action buttons on the event loop. The
        reply (our new notification id) is captured so ActionInvoked can be
        matched back to the currently shown notification."""
        import asyncio

        from dbus_fast import Message

        body = f"{song.artist} · {song.album}" if song.album else song.artist
        actions: list[str] = []
        for action_id, label in NOTIFY_ACTIONS:
            actions += [action_id, label]
        icon = str(art_path) if art_path is not None else "NaviTui"
        # replace-in-place is handled portably by `replaces_id` below; keep the
        # hints minimal (just normal urgency) — the old Unity-only
        # x-canonical-private-synchronous hint made some daemons swallow the
        # popup entirely.
        hints = {
            "urgency": _Variant("y", 1),  # 1 = normal
        }
        msg = Message(
            destination=NOTIFY_SERVICE,
            path=NOTIFY_PATH,
            interface=NOTIFY_SERVICE,
            member="Notify",
            signature="susssasa{sv}i",
            body=[
                "NaviTui",          # app_name
                self._last_id,      # replaces_id (0 first time, then reuse)
                icon,               # app_icon (cover path or name)
                song.title,         # summary
                body,               # body
                actions,            # actions
                hints,              # hints
                4000,               # expire_timeout (ms)
            ],
        )

        async def _send() -> None:
            try:
                reply = await self._bus.call(msg)
                if reply is not None and reply.body:
                    self._last_id = reply.body[0]
            except Exception:
                pass

        try:
            # keep a reference: an un-referenced task can be GC'd mid-flight
            task = asyncio.get_running_loop().create_task(_send())
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        except RuntimeError:
            pass

    def _notify_tool(self, song: Song, art_path: Path | None) -> None:
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

    def stop(self) -> None:
        if self._bus is not None:
            try:
                self._bus.disconnect()
            except Exception:
                pass
            self._bus = None


def _Variant(sig: str, value):
    from dbus_fast import Variant

    return Variant(sig, value)


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
                # activity_type 2 = "Listening to", so Discord reads
                # "Listening to <title>" rather than the default "Playing".
                # pypresence >= 4.3 (our pin) accepts this on update().
                "activity_type": 2,
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


LISTENBRAINZ_URL = "https://api.listenbrainz.org/1/submit-listens"


class ListenBrainz:
    """ListenBrainz scrobbling, entirely opt-in: needs a user token from
    listenbrainz.org/profile. Submits a "playing_now" listen on track start
    and a "single" listen once a track counts as played — mirroring the
    Subsonic scrobble it rides alongside. Degrades to a silent no-op: a
    missing token, an import failure, or any network trouble must never be a
    reason playback stalls.

    Network calls are async and best-effort. `submit` is awaited from the
    app's scrobble worker (never the UI thread); it swallows every error and
    returns whether the listen was accepted so a caller can buffer on failure.
    """

    def __init__(self, token: str) -> None:
        self._token = (token or "").strip()
        self._http = None
        if not self._token:
            return
        try:
            import httpx

            self._http = httpx.AsyncClient(timeout=10)
        except Exception:
            self._http = None

    @property
    def enabled(self) -> bool:
        return self._http is not None

    async def submit(self, song: Song, listened_at: int | None = None) -> bool:
        """A finished play: `listen_type` "single" with a listen timestamp.
        Returns True if ListenBrainz accepted it, False on any failure (so the
        caller may retry later). No-op (returns False) when disabled."""
        return await self._post(_lb_payload(song, "single", listened_at))

    async def now_playing(self, song: Song) -> bool:
        """A "playing_now" listen — no timestamp; ListenBrainz treats it as a
        transient "listening to" state, not a counted play."""
        return await self._post(_lb_payload(song, "playing_now", None))

    async def _post(self, payload: dict | None) -> bool:
        if self._http is None or payload is None:
            return False
        try:
            resp = await self._http.post(
                LISTENBRAINZ_URL,
                json=payload,
                headers={"Authorization": f"Token {self._token}"},
            )
            return resp.status_code == 200
        except Exception:
            return False

    async def close(self) -> None:
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:
                pass
            self._http = None


def _lb_payload(song: Song, listen_type: str, listened_at: int | None) -> dict | None:
    """Build the submit-listens body. ListenBrainz requires at minimum an
    `artist_name` and `track_name`; without both there's nothing worth
    sending, so return None and let the caller skip."""
    artist = (song.artist or "").strip()
    title = (song.title or "").strip()
    if not (artist and title):
        return None
    info: dict = {"artist_name": artist, "track_name": title}
    album = (song.album or "").strip()
    if album:
        info["release_name"] = album
    additional: dict = {"media_player": "NaviTui", "submission_client": "NaviTui"}
    if song.duration:
        additional["duration"] = int(song.duration)
    if getattr(song, "track", None):
        additional["tracknumber"] = int(song.track)
    info["additional_info"] = additional
    listen: dict = {"track_metadata": info}
    if listen_type == "single":
        listen["listened_at"] = int(listened_at if listened_at is not None else time.time())
    return {"listen_type": listen_type, "payload": [listen]}
