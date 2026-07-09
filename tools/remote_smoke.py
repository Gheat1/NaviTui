"""Headless smoke test for the remote-control API. Connects a real asyncio
client to the socket the app opened, runs a few commands + a subscribe, and
asserts the player/queue reacted. Uses FakeClient, ao='null'."""

import asyncio
import json
import os
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from screenshots import FakeClient  # noqa: E402
from navitui.app import NaviTuiApp  # noqa: E402


def sock_path() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or (Path.home() / ".cache")
    return Path(base) / "navitui" / "remote.sock"


async def rpc(reader, writer, cmd, args=None, rid=1):
    writer.write((json.dumps({"id": rid, "cmd": cmd, "args": args or {}}) + "\n").encode())
    await writer.drain()
    line = await reader.readline()
    return json.loads(line)


async def scenario(app):
    # wait for the socket to appear
    p = sock_path()
    for _ in range(100):
        if p.exists():
            break
        await asyncio.sleep(0.05)
    assert p.exists(), "socket never created"
    # perms: 0600 file inside 0700 dir
    assert oct(p.stat().st_mode & 0o777) == "0o600", oct(p.stat().st_mode & 0o777)

    reader, writer = await asyncio.open_unix_connection(str(p))

    r = await rpc(reader, writer, "ping")
    assert r["ok"] and r["result"]["pong"], r

    # load some songs into the tracks pane so search/enqueue/play have data
    app._songs = await app.client.get_all_songs()
    app.queue.set_songs(app._songs[:3], 0)

    r = await rpc(reader, writer, "state")
    st = r["result"]
    assert r["ok"] and len(st["queue"]) == 3, st
    assert st["repeat"] == "off" and st["shuffle"] is False, st

    # volume set
    r = await rpc(reader, writer, "volume", {"set": 55})
    assert r["ok"], r
    r = await rpc(reader, writer, "state")
    assert r["result"]["volume"] == 55, r["result"]["volume"]

    # volume adjust
    await rpc(reader, writer, "volume", {"delta": 10})
    r = await rpc(reader, writer, "state")
    assert r["result"]["volume"] == 65, r["result"]["volume"]

    # shuffle toggle reflected in queue state
    r = await rpc(reader, writer, "shuffle")
    assert r["ok"], r
    r = await rpc(reader, writer, "state")
    assert r["result"]["shuffle"] is True, r["result"]

    # repeat cycle off -> all
    await rpc(reader, writer, "repeat")
    r = await rpc(reader, writer, "state")
    assert r["result"]["repeat"] == "all", r["result"]

    # search hits the (fake) client
    r = await rpc(reader, writer, "search", {"query": app._songs[0].title.lower(), "limit": 5})
    assert r["ok"] and len(r["result"]["songs"]) >= 1, r
    hit = r["result"]["songs"][0]

    # enqueue that song "next"
    before = len(app.queue.songs)
    r = await rpc(reader, writer, "enqueue", {"song_id": hit["id"], "next": True})
    assert r["ok"] and r["result"]["queued"], r
    assert len(app.queue.songs) == before + 1, (before, len(app.queue.songs))

    # subscribe: first push is the primed state, then a push after next_track
    r = await rpc(reader, writer, "subscribe")
    assert r["ok"] and r["result"]["subscribed"], r
    push = json.loads(await asyncio.wait_for(reader.readline(), 2))
    assert push.get("event") == "state", push

    r = await rpc(reader, writer, "next")
    assert r["ok"], r
    # a state event should arrive from _announce -> publish
    push = json.loads(await asyncio.wait_for(reader.readline(), 2))
    assert push.get("event") == "state", push
    assert push["state"]["index"] == 1, push["state"]

    # unknown command
    r = await rpc(reader, writer, "frobnicate")
    assert not r["ok"] and "unknown" in r["error"], r

    writer.close()
    print("REMOTE_TEST_OK")


async def main():
    app = NaviTuiApp(client=FakeClient(), ao="null")
    async with app.run_test() as pilot:
        await scenario(app)
        await pilot.app.action_quit()


asyncio.run(main())
