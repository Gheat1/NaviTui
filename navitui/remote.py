"""Remote-control API — the local wire the CLI (#27) and MCP server (#26)
both sit on.

An asyncio server that lives on the app's own event loop (like `mpris`): a
client connects, sends newline-delimited JSON commands, and gets one JSON
response per line back. Every handler calls straight into the existing app
actions / player / queue on the loop — no new playback logic, no threads.

The server is fully optional: if it can't bind it degrades to a silent
no-op and NEVER stops the app from starting.

Transport
---------
By default a **unix-domain socket** under the user's runtime dir
(``$XDG_RUNTIME_DIR/navitui/remote.sock``, falling back to the cache dir),
created with ``0600`` perms inside a ``0700`` dir — unreachable off-machine,
no port, no dependency. On platforms without unix sockets (Windows) it binds
**127.0.0.1** on an ephemeral port and writes the chosen ``host:port`` to
``<runtime>/remote.addr`` so a local client can find it. A ``token`` from
config is required on TCP (and optional on the socket) — see `authenticate`.

Wire protocol (newline-delimited JSON, UTF-8)
---------------------------------------------
Each line the client sends is one request object::

    {"id": <any, optional>, "cmd": "<name>", "args": {<params>}}

Each line the server sends back is one response object::

    {"id": <echoed>, "ok": true,  "result": {<data>}}
    {"id": <echoed>, "ok": false, "error": "<message>"}

``id`` is echoed verbatim so a client can match replies (handy once
subscribe pushes are interleaved). ``args`` is optional and may be omitted
for commands that take none. Unknown commands return ``ok: false``.

If a ``token`` is configured, the FIRST request on a connection must be
``{"cmd": "auth", "args": {"token": "<token>"}}``; anything else is refused
until it succeeds. With no token configured, auth is skipped.

Commands
--------
Transport control (all return ``{"ok": true}`` with an empty/echoed result):

    play_pause                         toggle play/pause (resumes a restored queue)
    play                               resume, or start the current queue
    pause                              pause
    stop                              stop playback (clears the transport)
    next                               next track
    prev                               previous track (or restart if >4s in)
    seek       {"delta": <sec>}        seek relative by ±seconds
    seek       {"to": <sec>}           seek to an absolute position (seconds)
    volume     {"delta": <int>}        adjust volume by ±N (0..130)
    volume     {"set": <int>}          set volume to N (0..130)
    mute                               toggle mute
    shuffle                            toggle shuffle
    repeat                             cycle repeat  off → all → one

Library (compose on the existing client/queue):

    search     {"query": <str>, "limit": <int>?}
                                       -> {"songs": [...], "albums": [...], "artists": [...]}
    enqueue    {"song_id": <str>, "next": <bool>?}
                                       queue a searched song (by id) now/next

Query / push:

    state                              -> the now-playing snapshot (see `snapshot`)
    ping                               -> {"pong": true}
    auth       {"token": <str>}        -> {"authenticated": true}
    subscribe                          -> {"subscribed": true}, then an unsolicited
                                       {"event": "state", "state": {...}} line on every
                                       state change (and once immediately)
    unsubscribe                        stop the push stream

The ``state`` snapshot::

    {
      "playing": bool, "paused": bool, "active": bool,
      "position": float, "volume": int, "muted": bool,
      "shuffle": bool, "repeat": "off"|"all"|"one",
      "song": {"id","title","artist","album","album_id","duration",
               "starred","cover_art"} | null,
      "queue": [ {"id","title","artist","album","duration","current": bool}, ... ],
      "index": int
    }
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from navitui.models import Song

# commands that never need auth (so a client can hand over its token)
_NO_AUTH = {"auth", "ping"}


def _runtime_dir(fallback: Path) -> Path:
    """A per-user, private directory for the socket/addr file. Prefer
    $XDG_RUNTIME_DIR (tmpfs, 0700, cleaned on logout); fall back to the
    app cache dir when it's unset (macOS, cron)."""
    base = os.environ.get("XDG_RUNTIME_DIR")
    root = Path(base) if base else fallback
    d = root / "navitui"
    try:
        d.mkdir(parents=True, exist_ok=True)
        d.chmod(0o700)
    except OSError:
        pass
    return d


class Remote:
    """App-facing façade, mirroring `Mpris`: ``await start(controls, ...)``
    once, then `publish(snapshot)` whenever state changes. Every method is a
    safe no-op when the server never came up."""

    def __init__(self) -> None:
        self._server: asyncio.AbstractServer | None = None
        self._controls: dict[str, Callable[..., Any]] = {}
        self._snapshot: Callable[[], dict] | None = None
        self._token: str = ""
        self._subscribers: set[asyncio.Queue] = set()
        self._paths: list[Path] = []  # socket + addr file, unlinked on stop

    async def start(
        self,
        controls: dict[str, Callable[..., Any]],
        snapshot: Callable[[], dict],
        cache_dir: Path,
        token: str = "",
        enabled: bool = True,
    ) -> bool:
        """Bring the server up. Returns True on success. Any failure (port
        taken, no socket support, permissions) is swallowed — the app must
        start regardless."""
        if not enabled:
            return False
        self._controls = controls
        self._snapshot = snapshot
        self._token = token or ""
        rt = _runtime_dir(cache_dir)
        try:
            if hasattr(socket, "AF_UNIX") and not sys.platform.startswith("win"):
                sock_path = rt / "remote.sock"
                try:
                    sock_path.unlink()  # clear a stale socket from a crash
                except OSError:
                    pass
                self._server = await asyncio.start_unix_server(
                    self._handle, path=str(sock_path)
                )
                try:
                    sock_path.chmod(0o600)
                except OSError:
                    pass
                self._paths.append(sock_path)
            else:
                self._server = await asyncio.start_server(
                    self._handle, host="127.0.0.1", port=0
                )
                addr = self._server.sockets[0].getsockname()
                addr_path = rt / "remote.addr"
                addr_path.write_text(f"{addr[0]}:{addr[1]}")
                self._paths.append(addr_path)
            return True
        except Exception:
            self._server = None
            return False

    # ── one client connection ─────────────────────────────────────────
    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        authed = not self._token  # no token configured → already trusted
        queue: asyncio.Queue | None = None
        pusher: asyncio.Task | None = None
        try:
            while True:
                line = await reader.readline()
                if not line:  # client closed
                    break
                req = self._parse(line)
                if req is None:
                    self._write(writer, {"ok": False, "error": "bad json"})
                    continue
                rid = req.get("id")
                cmd = req.get("cmd")
                args = req.get("args") or {}
                if not isinstance(args, dict):
                    args = {}

                if not authed and cmd not in _NO_AUTH:
                    self._write(writer, {"id": rid, "ok": False, "error": "auth required"})
                    continue
                if cmd == "auth":
                    authed = args.get("token", "") == self._token
                    if authed:
                        self._write(writer, {"id": rid, "ok": True, "result": {"authenticated": True}})
                    else:
                        self._write(writer, {"id": rid, "ok": False, "error": "bad token"})
                    continue
                if cmd == "subscribe":
                    if queue is None:
                        queue = asyncio.Queue()
                        self._subscribers.add(queue)
                        pusher = asyncio.create_task(self._pump(queue, writer))
                        queue.put_nowait(self._state())  # prime with current state
                    self._write(writer, {"id": rid, "ok": True, "result": {"subscribed": True}})
                    continue
                if cmd == "unsubscribe":
                    if queue is not None:
                        self._subscribers.discard(queue)
                        queue.put_nowait(None)  # signal the pump to finish
                        queue = None
                    self._write(writer, {"id": rid, "ok": True, "result": {"unsubscribed": True}})
                    continue

                self._write(writer, await self._dispatch(rid, cmd, args))
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception:
            pass
        finally:
            if queue is not None:
                self._subscribers.discard(queue)
                queue.put_nowait(None)
            if pusher is not None:
                pusher.cancel()
            try:
                writer.close()
            except Exception:
                pass

    async def _dispatch(self, rid: Any, cmd: Any, args: dict) -> dict:
        """Run one command on the loop. Handlers are the app's own actions;
        we only translate args and never touch the UI ourselves. A handler
        may return a dict, None, or an awaitable (e.g. search hits the API)."""
        try:
            if cmd == "ping":
                return {"id": rid, "ok": True, "result": {"pong": True}}
            if cmd == "state":
                return {"id": rid, "ok": True, "result": self._state()}
            handler = self._controls.get(cmd)
            if handler is None:
                return {"id": rid, "ok": False, "error": f"unknown command: {cmd!r}"}
            result = handler(args)
            if asyncio.iscoroutine(result):
                result = await result
            return {"id": rid, "ok": True, "result": result if isinstance(result, dict) else {}}
        except Exception as e:
            return {"id": rid, "ok": False, "error": f"{type(e).__name__}: {e}"}

    def _state(self) -> dict:
        if self._snapshot is None:
            return {}
        try:
            return self._snapshot()
        except Exception:
            return {}

    # ── push ───────────────────────────────────────────────────────────
    async def _pump(self, queue: asyncio.Queue, writer: asyncio.StreamWriter) -> None:
        """Drain a subscriber's queue onto the socket until it's closed."""
        try:
            while True:
                state = await queue.get()
                if state is None:  # unsubscribe / teardown sentinel
                    return
                self._write(writer, {"event": "state", "state": state})
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            return
        except Exception:
            return

    def publish(self, snapshot: dict | None = None) -> None:
        """Fan a fresh state snapshot to every subscriber. Called from the
        app's `_announce` — always on the loop, so no locking needed."""
        if not self._subscribers:
            return
        state = snapshot if snapshot is not None else self._state()
        for q in self._subscribers:
            try:
                q.put_nowait(state)
            except Exception:
                pass

    # ── framing helpers ────────────────────────────────────────────────
    @staticmethod
    def _parse(line: bytes) -> dict | None:
        try:
            obj = json.loads(line.decode("utf-8"))
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None

    @staticmethod
    def _write(writer: asyncio.StreamWriter, obj: dict) -> None:
        try:
            writer.write((json.dumps(obj) + "\n").encode("utf-8"))
        except Exception:
            pass

    # ── shutdown ────────────────────────────────────────────────────────
    def stop(self) -> None:
        """Close the listener and drop every subscriber. Safe to call when
        the server never started."""
        for q in list(self._subscribers):
            try:
                q.put_nowait(None)
            except Exception:
                pass
        self._subscribers.clear()
        if self._server is not None:
            try:
                self._server.close()
            except Exception:
                pass
            self._server = None
        for p in self._paths:
            try:
                p.unlink()
            except OSError:
                pass
        self._paths = []


# ── snapshot builder (pure; used by the app to feed `snapshot`) ─────────
def build_snapshot(
    song: "Song | None",
    songs: list["Song"],
    index: int,
    position: float,
    volume: int,
    muted: bool,
    playing: bool,
    active: bool,
    shuffle: bool,
    repeat: str,
) -> dict:
    """Assemble the now-playing snapshot from raw app state. Kept here (not
    in app.py) so #26/#27 have a single source of truth for the shape."""
    return {
        "playing": playing,
        "paused": active and not playing,
        "active": active,
        "position": round(float(position), 2),
        "volume": int(volume),
        "muted": bool(muted),
        "shuffle": bool(shuffle),
        "repeat": repeat,
        "song": _song_dict(song) if song is not None else None,
        "index": index,
        "queue": [
            {
                "id": s.id,
                "title": s.title,
                "artist": s.artist,
                "album": s.album,
                "duration": s.duration,
                "current": i == index,
            }
            for i, s in enumerate(songs)
        ],
    }


def _song_dict(s: "Song") -> dict:
    return {
        "id": s.id,
        "title": s.title,
        "artist": s.artist,
        "album": s.album,
        "album_id": s.album_id,
        "duration": s.duration,
        "starred": s.starred,
        "cover_art": s.cover_art,
    }
