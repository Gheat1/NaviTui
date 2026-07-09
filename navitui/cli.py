"""`navitui-remote` — a scriptable CLI that drives a *running* NaviTui.

This is a separate, short-lived process (issue #27): it connects to a live
app over the unix socket the app opened (see `remote.py`), sends one command,
prints the result, and exits. It never imports or runs the Textual app and
never touches playback logic itself — it only speaks the NDJSON wire protocol.

Stdlib only: `argparse` for the command surface, `asyncio` for the unix-socket
(or TCP-fallback) client. It reuses `remote._runtime_dir` so the socket path is
resolved exactly the way the server picks it, and reads the optional
`remote_token` from `player.toml` via `config.load` so an authed instance just
works.

Exit codes: 0 on success, 1 on a command error (server said ``ok: false``),
2 when no running instance is found / can't connect. That makes it safe to
bind to global hotkeys or drop in a script.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from ricekit.storage import AppDirs

from navitui import config as configmod
from navitui.remote import _runtime_dir

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NO_INSTANCE = 2


class RemoteError(Exception):
    """The server refused a command (returned ``ok: false``)."""


class NoInstance(Exception):
    """No reachable NaviTui — nothing bound the socket / address."""


# ── connection ─────────────────────────────────────────────────────────────
def _dirs() -> AppDirs:
    return AppDirs("navitui")


def _socket_path() -> Path:
    """Where the server put its unix socket (same helper the server uses)."""
    return _runtime_dir(_dirs().cache_dir) / "remote.sock"


def _addr_path() -> Path:
    """The TCP fallback address file (Windows / no AF_UNIX)."""
    return _runtime_dir(_dirs().cache_dir) / "remote.addr"


def _load_token() -> str:
    try:
        cfg = configmod.load(_dirs().config_file.parent)
        return str(cfg.get("remote_token", "") or "")
    except Exception:
        return ""


class Client:
    """A one-shot NDJSON client over the app's unix socket (or TCP fallback).

    Opens the connection, authenticates if a token is configured, then lets the
    caller `request()` one command at a time. Not concurrent — replies are read
    in order, which is all a CLI needs.
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._id = 0

    @classmethod
    async def connect(cls) -> "Client":
        reader, writer = await _open()
        client = cls(reader, writer)
        token = _load_token()
        if token:
            await client.request("auth", {"token": token})
        return client

    async def request(self, cmd: str, args: dict | None = None) -> Any:
        self._id += 1
        rid = self._id
        payload = {"id": rid, "cmd": cmd}
        if args:
            payload["args"] = args
        self._writer.write((json.dumps(payload) + "\n").encode("utf-8"))
        await self._writer.drain()
        line = await self._reader.readline()
        if not line:
            raise NoInstance("connection closed by the app")
        try:
            resp = json.loads(line.decode("utf-8"))
        except Exception as exc:  # pragma: no cover - defensive
            raise RemoteError(f"bad reply: {exc}") from exc
        if not resp.get("ok"):
            raise RemoteError(str(resp.get("error", "command failed")))
        return resp.get("result", {})

    async def close(self) -> None:
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except Exception:
            pass


async def _open() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Open the unix socket, or the TCP fallback if that's what the app wrote.
    Raises `NoInstance` when nothing is listening."""
    sock = _socket_path()
    if sock.exists():
        try:
            return await asyncio.open_unix_connection(path=str(sock))
        except (ConnectionRefusedError, FileNotFoundError, OSError) as exc:
            raise NoInstance(str(exc)) from exc
    addr = _addr_path()
    if addr.exists():
        try:
            host, _, port = addr.read_text().strip().rpartition(":")
            return await asyncio.open_connection(host, int(port))
        except (ConnectionRefusedError, OSError, ValueError) as exc:
            raise NoInstance(str(exc)) from exc
    raise NoInstance("no socket or address file")


# ── formatting ─────────────────────────────────────────────────────────────
def _fmt_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def format_status(state: dict) -> str:
    """One-line now-playing: ``▸ Title — Artist [1:23/4:56]  vol 65 shuffle``."""
    song = state.get("song")
    if not state.get("active") or not song:
        vol = int(state.get("volume", 0))
        tail = f"vol {vol}"
        if state.get("muted"):
            tail += " muted"
        return f"■ (stopped)  {tail}"
    icon = "▸" if state.get("playing") else "‖"  # ▸ / ‖
    title = song.get("title") or "?"
    artist = song.get("artist") or ""
    head = f"{icon} {title}"
    if artist:
        head += f" — {artist}"  # em dash
    pos = _fmt_time(state.get("position", 0))
    dur = _fmt_time(song.get("duration", 0))
    parts = [head, f"[{pos}/{dur}]"]
    tail = [f"vol {int(state.get('volume', 0))}"]
    if state.get("muted"):
        tail.append("muted")
    if state.get("shuffle"):
        tail.append("shuffle")
    repeat = state.get("repeat", "off")
    if repeat and repeat != "off":
        tail.append(f"repeat:{repeat}")
    return "  ".join(parts) + "  " + " ".join(tail)


def _fmt_hit(song: dict) -> str:
    dur = _fmt_time(song.get("duration", 0))
    artist = song.get("artist") or ""
    who = f" — {artist}" if artist else ""
    return f"{song.get('id')}  {song.get('title', '?')}{who}  [{dur}]"


# ── subcommands ────────────────────────────────────────────────────────────
async def _run(args: argparse.Namespace) -> int:
    client = await Client.connect()
    try:
        return await _dispatch(client, args)
    finally:
        await client.close()


async def _dispatch(client: Client, args: argparse.Namespace) -> int:
    cmd = args.command

    if cmd in ("status", "now"):
        state = await client.request("state")
        print(format_status(state))
        return EXIT_OK

    if cmd == "toggle":
        await client.request("play_pause")
        return EXIT_OK

    if cmd in ("pause", "stop", "next", "prev", "mute", "shuffle", "repeat"):
        await client.request(cmd)
        return EXIT_OK

    if cmd == "play":
        # `play` with a query = search + enqueue-next + play the top song;
        # bare `play` just resumes. Composes cleanly on remote cmds.
        if args.query:
            return await _play_query(client, " ".join(args.query))
        await client.request("play")
        return EXIT_OK

    if cmd == "seek":
        await client.request("seek", _seek_args(args.amount))
        return EXIT_OK

    if cmd == "volume":
        result = await client.request("volume", _volume_args(args.amount))
        if isinstance(result, dict) and "volume" in result:
            print(f"vol {result['volume']}")
        return EXIT_OK

    if cmd == "search":
        res = await client.request("search", {"query": " ".join(args.query)})
        songs = res.get("songs", []) if isinstance(res, dict) else []
        if not songs:
            print("no results", file=sys.stderr)
            return EXIT_OK
        for song in songs:
            print(_fmt_hit(song))
        return EXIT_OK

    if cmd == "enqueue":
        res = await client.request(
            "enqueue", {"song_id": args.song_id, "next": bool(args.next)}
        )
        if isinstance(res, dict) and res.get("queued"):
            print(f"queued {res.get('title', args.song_id)}")
            return EXIT_OK
        print("could not queue that id", file=sys.stderr)
        return EXIT_ERROR

    # argparse guarantees a valid subcommand, so this is unreachable
    print(f"unknown command: {cmd}", file=sys.stderr)  # pragma: no cover
    return EXIT_ERROR  # pragma: no cover


async def _play_query(client: Client, query: str) -> int:
    """Search, then enqueue-next and play the top song hit."""
    res = await client.request("search", {"query": query, "limit": 5})
    songs = res.get("songs", []) if isinstance(res, dict) else []
    if not songs:
        print(f"no song matched {query!r}", file=sys.stderr)
        return EXIT_ERROR
    top = songs[0]
    enq = await client.request("enqueue", {"song_id": top["id"], "next": True})
    if not (isinstance(enq, dict) and enq.get("queued")):
        print("could not queue the match", file=sys.stderr)
        return EXIT_ERROR
    # advance onto the just-queued track and make sure we're playing
    await client.request("next")
    await client.request("play")
    print(f"▸ {top.get('title', '?')} — {top.get('artist', '')}".rstrip(" — "))
    return EXIT_OK


def _seek_args(amount: str) -> dict:
    """``to=90`` -> absolute; ``+15`` / ``-15`` / ``15`` -> relative delta."""
    amount = amount.strip()
    if amount.startswith("to="):
        return {"to": float(amount[3:])}
    return {"delta": float(amount)}


def _volume_args(amount: str) -> dict:
    """``set=65`` -> absolute; ``+5`` / ``-5`` / ``5`` -> relative delta."""
    amount = amount.strip()
    if amount.startswith("set="):
        return {"set": int(amount[4:])}
    return {"delta": int(amount)}


# ── entry point ────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="navitui-remote",
        description="Control a running NaviTui over its local socket.",
    )
    sub = p.add_subparsers(dest="command", required=True, metavar="<command>")

    sub.add_parser("status", help="print the now-playing line")
    sub.add_parser("now", help="alias for status")
    play = sub.add_parser("play", help="resume, or search+play a query")
    play.add_argument("query", nargs="*", help="optional song query to play")
    sub.add_parser("pause", help="pause playback")
    sub.add_parser("toggle", help="toggle play/pause")
    sub.add_parser("stop", help="stop playback")
    sub.add_parser("next", help="next track")
    sub.add_parser("prev", help="previous track")

    seek = sub.add_parser("seek", help="seek by ±secs, or to=<secs>")
    seek.add_argument("amount", help="e.g. +15, -15, or to=90")

    vol = sub.add_parser("volume", help="volume by ±n, or set=<n>")
    vol.add_argument("amount", help="e.g. +5, -5, or set=65")

    sub.add_parser("mute", help="toggle mute")
    sub.add_parser("shuffle", help="toggle shuffle")
    sub.add_parser("repeat", help="cycle repeat off/all/one")

    search = sub.add_parser("search", help="search the library, list song hits")
    search.add_argument("query", nargs="+", help="search terms")

    enq = sub.add_parser("enqueue", help="queue a song by id")
    enq.add_argument("song_id", help="song id from a search result")
    enq.add_argument("--next", action="store_true", help="play it next, not last")

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except NoInstance:
        print("no running NaviTui found", file=sys.stderr)
        return EXIT_NO_INSTANCE
    except RemoteError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:  # pragma: no cover
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
