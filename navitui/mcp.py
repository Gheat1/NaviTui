"""MCP server — control a running NaviTui from Claude Code / any MCP client.

A *separate* process from the TUI. It never imports or runs the Textual app;
it just connects to a **running** NaviTui instance over the unix-domain socket
that `navitui.remote` exposes (``$XDG_RUNTIME_DIR/navitui/remote.sock``,
falling back to the cache dir) and speaks the same newline-delimited JSON
protocol::

    -> {"id": <n>, "cmd": "<name>", "args": {...}}
    <- {"id": <n>, "ok": true, "result": {...}}  |  {"ok": false, "error": ...}

Each tool below maps onto exactly one remote command. Connection failures
(the app isn't running, the socket is missing, auth is wrong) become tool
*errors* — a clear string — never a crash.

Run it over stdio (the MCP norm)::

    navitui-mcp            # after `pip install "navitui[mcp]"`

Point an MCP client at that command. If the app was started with a
``remote_token`` set (see player.toml), export ``NAVITUI_REMOTE_TOKEN`` so the
server can authenticate; otherwise the socket needs no token.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

# The MCP SDK is an optional extra: `pip install "navitui[mcp]"`. Import it
# lazily-but-carefully so this module still *imports* without it (tests, tab
# completion, --help) and fails only when you actually try to run the server.
try:
    from mcp.server.fastmcp import FastMCP  # type: ignore
except Exception:  # pragma: no cover - exercised only when mcp is absent
    FastMCP = None  # type: ignore

# Reuse the app's own socket-path resolver so we always look in the same place.
from navitui.remote import _runtime_dir

_TOKEN_ENV = "NAVITUI_REMOTE_TOKEN"


class RemoteError(Exception):
    """A user-facing failure to talk to the running NaviTui instance."""


def _cache_dir() -> Path:
    """The app's cache dir, resolved exactly as the running app does (via
    ricekit's AppDirs) so `_runtime_dir`'s fallback matches. Degrades to a
    sensible XDG default if ricekit isn't importable here."""
    try:
        from ricekit.storage import AppDirs

        return AppDirs("navitui").cache_dir
    except Exception:
        base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
        return Path(base) / "navitui"


def _socket_path() -> Path:
    return _runtime_dir(_cache_dir()) / "remote.sock"


def _token() -> str:
    """Shared secret for the socket, if the app was started with one. Read
    from the env first (how you'd wire it into an MCP client config), then
    from player.toml as a convenience for a same-user setup."""
    tok = os.environ.get(_TOKEN_ENV)
    if tok:
        return tok
    try:
        from ricekit.storage import AppDirs

        from navitui import config as configmod

        cfg = configmod.load(AppDirs("navitui").config_file.parent)
        return str(cfg.get("remote_token") or "")
    except Exception:
        return ""


class RemoteClient:
    """A minimal one-shot NDJSON client for the remote socket.

    Each `call` opens a fresh connection, (optionally) authenticates, sends
    one request, reads one matching reply, and closes. Short-lived by design:
    MCP tool calls are infrequent and this keeps state (and failure modes)
    trivial — no pooling, no lingering fds.
    """

    def __init__(self, path: Path | None = None, token: str | None = None) -> None:
        self._path = path or _socket_path()
        self._token = token if token is not None else _token()

    async def call(self, cmd: str, args: dict | None = None) -> dict:
        """Send one command, return the parsed ``result`` dict. Raises
        `RemoteError` with a readable message on any transport/protocol
        failure or a server-side ``ok: false``."""
        if not self._path.exists():
            raise RemoteError(
                f"NaviTui isn't running (no socket at {self._path}). "
                "Start the app first; the remote API comes up with it."
            )
        try:
            reader, writer = await asyncio.open_unix_connection(str(self._path))
        except (FileNotFoundError, ConnectionRefusedError):
            raise RemoteError(
                f"NaviTui isn't running (socket {self._path} is dead). "
                "Start the app and try again."
            )
        except OSError as e:
            raise RemoteError(f"can't reach NaviTui: {e}")
        try:
            if self._token:
                await self._send(writer, {"cmd": "auth", "args": {"token": self._token}})
                reply = await self._recv(reader)
                if not reply.get("ok"):
                    raise RemoteError(
                        "authentication failed — set "
                        f"{_TOKEN_ENV} to the app's remote_token"
                    )
            await self._send(writer, {"id": 1, "cmd": cmd, "args": args or {}})
            reply = await self._recv(reader)
            if not reply.get("ok"):
                raise RemoteError(str(reply.get("error") or "command failed"))
            result = reply.get("result")
            return result if isinstance(result, dict) else {}
        finally:
            try:
                writer.close()
            except Exception:
                pass

    @staticmethod
    async def _send(writer: asyncio.StreamWriter, obj: dict) -> None:
        writer.write((json.dumps(obj) + "\n").encode("utf-8"))
        await writer.drain()

    @staticmethod
    async def _recv(reader: asyncio.StreamReader) -> dict:
        """Read one line, skipping any interleaved push events (subscribe
        pushes carry ``event`` and no ``ok``) that aren't our reply."""
        while True:
            line = await reader.readline()
            if not line:
                raise RemoteError("connection closed by NaviTui before a reply")
            try:
                obj = json.loads(line.decode("utf-8"))
            except Exception:
                continue
            if isinstance(obj, dict) and "event" in obj and "ok" not in obj:
                continue  # an unsolicited state push — not our answer
            if isinstance(obj, dict):
                return obj


# ── MCP server ──────────────────────────────────────────────────────────────
def build_server(client: RemoteClient | None = None) -> "FastMCP":
    """Create the FastMCP server with every tool wired to `client`. Kept as a
    factory so tests can inject a client pointed at a fake socket."""
    if FastMCP is None:  # pragma: no cover
        raise RuntimeError(
            "the MCP SDK isn't installed. Run: pip install \"navitui[mcp]\""
        )

    rc = client or RemoteClient()
    server = FastMCP("navitui")

    async def _run(cmd: str, args: dict | None = None) -> dict:
        """Dispatch a remote command, turning our RemoteError into the plain
        error dict MCP surfaces to the caller instead of raising."""
        try:
            return await rc.call(cmd, args)
        except RemoteError as e:
            return {"error": str(e)}

    @server.tool()
    async def now_playing() -> dict:
        """Get the full now-playing snapshot from the running NaviTui:
        playing/paused/active, position, volume, mute, shuffle, repeat, the
        current song (title/artist/album/duration/...), the queue and index."""
        return await _run("state")

    @server.tool()
    async def get_state() -> dict:
        """Alias of now_playing — the raw player state snapshot."""
        return await _run("state")

    @server.tool()
    async def play_pause() -> dict:
        """Toggle play/pause (resumes a restored queue)."""
        return await _run("play_pause")

    @server.tool()
    async def play() -> dict:
        """Resume playback, or start the current queue."""
        return await _run("play")

    @server.tool()
    async def pause() -> dict:
        """Pause playback."""
        return await _run("pause")

    @server.tool()
    async def stop() -> dict:
        """Stop playback and clear the transport."""
        return await _run("stop")

    @server.tool()
    async def next_track() -> dict:
        """Skip to the next track in the queue."""
        return await _run("next")

    @server.tool()
    async def prev_track() -> dict:
        """Go to the previous track (or restart the current one if >4s in)."""
        return await _run("prev")

    @server.tool()
    async def seek(delta: float | None = None, to: float | None = None) -> dict:
        """Seek. Pass `delta` to move relative by ±seconds, or `to` for an
        absolute position in seconds. If both are given, `to` wins."""
        if to is not None:
            return await _run("seek", {"to": float(to)})
        if delta is not None:
            return await _run("seek", {"delta": float(delta)})
        return {"error": "seek needs either `delta` (±sec) or `to` (abs sec)"}

    @server.tool()
    async def set_volume(level: int | None = None, delta: int | None = None) -> dict:
        """Set or adjust volume (0..130). Pass `level` for an absolute value,
        or `delta` to change by ±N. If both are given, `level` wins."""
        if level is not None:
            return await _run("volume", {"set": int(level)})
        if delta is not None:
            return await _run("volume", {"delta": int(delta)})
        return {"error": "set_volume needs either `level` (abs) or `delta` (±)"}

    @server.tool()
    async def mute() -> dict:
        """Toggle mute."""
        return await _run("mute")

    @server.tool()
    async def toggle_shuffle() -> dict:
        """Toggle shuffle on/off."""
        return await _run("shuffle")

    @server.tool()
    async def cycle_repeat() -> dict:
        """Cycle the repeat mode: off -> all -> one."""
        return await _run("repeat")

    @server.tool()
    async def search(query: str, limit: int | None = None) -> dict:
        """Search the library. Returns {"songs": [...], "albums": [...],
        "artists": [...]}. Song ids from here feed `enqueue`."""
        args: dict[str, Any] = {"query": query}
        if limit is not None:
            args["limit"] = int(limit)
        return await _run("search", args)

    @server.tool()
    async def enqueue(song_id: str, next: bool = False) -> dict:
        """Queue a song by its id (from `search`). Set `next=True` to play it
        immediately after the current track instead of at the end."""
        return await _run("enqueue", {"song_id": song_id, "next": bool(next)})

    return server


def main() -> None:
    """Console entry point (``navitui-mcp``): serve the tools over stdio."""
    if FastMCP is None:
        raise SystemExit(
            "the MCP SDK isn't installed. Run: pip install \"navitui[mcp]\""
        )
    build_server().run()


if __name__ == "__main__":  # pragma: no cover
    main()
