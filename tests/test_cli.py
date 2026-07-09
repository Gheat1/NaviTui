"""Stdlib-only tests for the `navitui-remote` CLI (#27).

The repo ships no test framework, so this is a self-contained runner: `python
tests/test_cli.py`. Each case spins up a tiny asyncio unix-socket server that
speaks the remote.py NDJSON protocol, points the CLI's socket-path resolution
at it, runs a subcommand through `main()`, and asserts the server saw the right
command/args and the CLI printed the parsed result. No Textual app, no network.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import sys
import tempfile
import threading
from pathlib import Path

from navitui import cli


class FakeServer:
    """A minimal remote.py-speaking server on its own loop/thread, bound to a
    temp unix socket. Records every request; replies from a canned table."""

    def __init__(self, sock_path: Path, replies: dict) -> None:
        self.sock_path = sock_path
        self.replies = replies
        self.requests: list[dict] = []
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._ready = threading.Event()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._serve())
        self._loop.run_forever()

    async def _serve(self) -> None:
        self._server = await asyncio.start_unix_server(self._handle, path=str(self.sock_path))
        self._ready.set()

    async def _handle(self, reader, writer) -> None:
        while True:
            line = await reader.readline()
            if not line:
                break
            req = json.loads(line.decode())
            self.requests.append(req)
            result = self.replies.get(req.get("cmd"), {})
            writer.write(
                (json.dumps({"id": req.get("id"), "ok": True, "result": result}) + "\n").encode()
            )
            await writer.drain()
        writer.close()

    def start(self) -> None:
        self._thread.start()
        self._ready.wait(2)

    def stop(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)

    def cmds(self) -> list[str]:
        return [r["cmd"] for r in self.requests]


# ── tiny harness ────────────────────────────────────────────────────────────
_ORIG = (cli._socket_path, cli._addr_path, cli._load_token)


def _point_at(sock: Path, tmp: Path) -> None:
    cli._socket_path = lambda: sock
    cli._addr_path = lambda: tmp / "remote.addr"
    cli._load_token = lambda: ""


def _restore() -> None:
    cli._socket_path, cli._addr_path, cli._load_token = _ORIG


def run(argv, replies=None):
    """Run `main(argv)` against a fresh fake server; return (rc, out, err, srv)."""
    tmp = Path(tempfile.mkdtemp())
    sock = tmp / "remote.sock"
    srv = FakeServer(sock, replies or {})
    srv.start()
    _point_at(sock, tmp)
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(argv)
    finally:
        srv.stop()
        _restore()
    return rc, out.getvalue(), err.getvalue(), srv


_passed = 0
_failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {name}")
    else:
        _failed += 1
        print(f"  FAIL {name}  {detail}")


# ── cases ───────────────────────────────────────────────────────────────────
def test_status():
    rc, out, _, srv = run(["status"], {
        "state": {
            "active": True, "playing": True, "position": 83, "volume": 65,
            "shuffle": True, "repeat": "all", "muted": False,
            "song": {"title": "Song", "artist": "Artist", "duration": 296},
        }
    })
    check("status rc", rc == cli.EXIT_OK)
    check("status text", "Song" in out and "Artist" in out and "[1:23/4:56]" in out, out)
    check("status flags", "vol 65" in out and "shuffle" in out and "repeat:all" in out, out)
    check("status cmd", srv.cmds() == ["state"], srv.cmds())


def test_toggle():
    rc, *_, srv = run(["toggle"])
    check("toggle", rc == cli.EXIT_OK and srv.cmds() == ["play_pause"], srv.cmds())


def test_passthrough():
    for sub in ("pause", "stop", "next", "prev", "mute", "shuffle", "repeat"):
        rc, *_, srv = run([sub])
        check(f"passthrough {sub}", rc == cli.EXIT_OK and srv.cmds() == [sub], srv.cmds())


def test_seek():
    _, _, _, s1 = run(["seek", "+15"])
    _, _, _, s2 = run(["seek", "to=90"])
    check("seek delta", s1.requests[0]["args"] == {"delta": 15.0}, s1.requests[0])
    check("seek to", s2.requests[0]["args"] == {"to": 90.0}, s2.requests[0])


def test_volume():
    _, _, _, s1 = run(["volume", "-5"], {"volume": {"volume": 70}})
    rc, out, _, s2 = run(["volume", "set=70"], {"volume": {"volume": 70}})
    check("vol delta", s1.requests[0]["args"] == {"delta": -5}, s1.requests[0])
    check("vol set", s2.requests[0]["args"] == {"set": 70}, s2.requests[0])
    check("vol print", "vol 70" in out, out)


def test_search():
    rc, out, _, srv = run(["search", "hello", "world"], {
        "search": {"songs": [
            {"id": "s1", "title": "One", "artist": "A", "duration": 60},
            {"id": "s2", "title": "Two", "artist": "B", "duration": 125},
        ]}
    })
    check("search rc", rc == cli.EXIT_OK)
    check("search hit1", "s1  One — A  [1:00]" in out, out)
    check("search hit2", "s2  Two — B  [2:05]" in out, out)
    check("search query", srv.requests[0]["args"] == {"query": "hello world"}, srv.requests[0])


def test_enqueue():
    rc, out, _, srv = run(["enqueue", "abc", "--next"], {"enqueue": {"queued": True, "title": "Track"}})
    check("enqueue rc", rc == cli.EXIT_OK)
    check("enqueue args", srv.requests[0]["args"] == {"song_id": "abc", "next": True}, srv.requests[0])
    check("enqueue print", "queued Track" in out, out)


def test_play_bare():
    rc, *_, srv = run(["play"])
    check("play bare", rc == cli.EXIT_OK and srv.cmds() == ["play"], srv.cmds())


def test_play_query():
    rc, out, _, srv = run(["play", "some", "song"], {
        "search": {"songs": [{"id": "top", "title": "Hit", "artist": "X", "duration": 100}]},
        "enqueue": {"queued": True, "title": "Hit"},
    })
    check("play query rc", rc == cli.EXIT_OK)
    check("play query chain", srv.cmds() == ["search", "enqueue", "next", "play"], srv.cmds())
    check("play query search args", srv.requests[0]["args"] == {"query": "some song", "limit": 5}, srv.requests[0])
    check("play query enq args", srv.requests[1]["args"] == {"song_id": "top", "next": True}, srv.requests[1])
    check("play query print", "Hit" in out, out)


def test_play_query_no_match():
    rc, _, err, _ = run(["play", "nothing"], {"search": {"songs": []}})
    check("play no-match rc", rc == cli.EXIT_ERROR)
    check("play no-match err", "no song matched" in err, err)


def test_no_instance():
    tmp = Path(tempfile.mkdtemp())
    cli._socket_path = lambda: tmp / "nope.sock"
    cli._addr_path = lambda: tmp / "nope.addr"
    cli._load_token = lambda: ""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(["status"])
    _restore()
    check("no-instance rc", rc == cli.EXIT_NO_INSTANCE)
    check("no-instance msg", "no running NaviTui found" in err.getvalue(), err.getvalue())


def test_server_error():
    """A server that replies ok:false maps to EXIT_ERROR + stderr message."""
    tmp = Path(tempfile.mkdtemp())
    sock = tmp / "remote.sock"
    loop = asyncio.new_event_loop()

    async def handle(reader, writer):
        line = await reader.readline()
        req = json.loads(line.decode())
        writer.write((json.dumps({"id": req.get("id"), "ok": False, "error": "boom"}) + "\n").encode())
        await writer.drain()
        writer.close()

    loop.run_until_complete(asyncio.start_unix_server(handle, path=str(sock)))
    threading.Thread(target=loop.run_forever, daemon=True).start()
    _point_at(sock, tmp)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(["next"])
    _restore()
    loop.call_soon_threadsafe(loop.stop)
    check("server-error rc", rc == cli.EXIT_ERROR)
    check("server-error msg", "boom" in err.getvalue(), err.getvalue())


def main() -> int:
    for fn in (
        test_status, test_toggle, test_passthrough, test_seek, test_volume,
        test_search, test_enqueue, test_play_bare, test_play_query,
        test_play_query_no_match, test_no_instance, test_server_error,
    ):
        print(fn.__name__)
        fn()
    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
