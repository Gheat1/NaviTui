"""Offline mutation queue — buffer library writes, replay them on reconnect.

Stars, ratings and scrobbles are *optimistic*: the local cache is updated the
instant the user acts, so the UI never waits on the network. When the server
is unreachable (or offline mode is on) the write itself is parked here instead
of surfacing an error, then flushed in order the next time we know we're online
(app start, offline-mode off, a successful auto-refresh).

The queue is a plain list of op dicts persisted through AppDirs' cache so it
survives a restart. No UI, no network, no Textual in here — `app.py` owns the
worker that drains it. Redundant ops on the same target collapse to the user's
*last* intent (star→unstar→star ⇒ one star; two ratings ⇒ the latest), which is
correctness-preserving because these endpoints are idempotent set-operations.
Scrobbles are the exception: each is a distinct play event, so they only
de-dupe on an exact (song, submission) match.
"""

from __future__ import annotations

from typing import Any, Callable

CACHE_KEY = "mutations"


class MutationQueue:
    """A persisted FIFO of pending library mutations.

    `client` is any object exposing the same async `set_star` / `set_rating` /
    `scrobble` as `SubsonicClient`; `flush` calls it and drops each op on
    success, keeping the rest on the first failure so ordering is preserved.
    """

    def __init__(self, load: Callable[[], dict | None], save: Callable[[dict], None]) -> None:
        self._load = load
        self._save = save
        self._ops: list[dict[str, Any]] = []
        cached = load()
        if cached:
            self._ops = [op for op in cached.get("ops", []) if _valid(op)]

    def __len__(self) -> int:
        return len(self._ops)

    @property
    def pending(self) -> int:
        return len(self._ops)

    def _persist(self) -> None:
        self._save({"ops": self._ops})

    # ── enqueue (collapsing redundant intent) ─────────────────────────
    def star(self, item_id: str, kind: str, star: bool) -> None:
        # last write to the same target wins: drop any earlier star/unstar of it
        self._ops = [
            op for op in self._ops
            if not (op["op"] == "star" and op["item_id"] == item_id and op["kind"] == kind)
        ]
        self._ops.append({"op": "star", "item_id": item_id, "kind": kind, "star": bool(star)})
        self._persist()

    def rate(self, song_id: str, rating: int) -> None:
        self._ops = [
            op for op in self._ops
            if not (op["op"] == "rate" and op["song_id"] == song_id)
        ]
        self._ops.append({"op": "rate", "song_id": song_id, "rating": int(rating)})
        self._persist()

    def scrobble(self, song_id: str, submission: bool) -> None:
        # a scrobble is a play event, not a set-operation: only fold an exact
        # duplicate (same song, same submission flag) that hasn't flushed yet
        for op in self._ops:
            if op["op"] == "scrobble" and op["song_id"] == song_id and op["submission"] == bool(submission):
                return
        self._ops.append({"op": "scrobble", "song_id": song_id, "submission": bool(submission)})
        self._persist()

    # ── flush ─────────────────────────────────────────────────────────
    async def flush(self, client, on_network_error: Callable[[Exception], bool]) -> int:
        """Replay queued ops in order against `client`, dropping each on success.

        `on_network_error(exc)` classifies a raised exception: return True if it
        looks like connectivity trouble (stop; keep this and the rest for next
        time) or False if the server rejected the op (a bad id, say — drop it so
        it can't wedge the queue forever). Returns the number of ops flushed.
        """
        flushed = 0
        while self._ops:
            op = self._ops[0]
            try:
                await _apply(client, op)
            except Exception as exc:  # noqa: BLE001 — classify below
                if on_network_error(exc):
                    break  # still offline: preserve order, try again later
                # server rejected it: drop and move on so one bad op can't stick
            self._ops.pop(0)
            flushed += 1
        self._persist()
        return flushed


def _valid(op: dict) -> bool:
    """Guard against a corrupt/partial persisted op wedging load."""
    kind = op.get("op")
    if kind == "star":
        return {"item_id", "kind", "star"} <= op.keys()
    if kind == "rate":
        return {"song_id", "rating"} <= op.keys()
    if kind == "scrobble":
        return {"song_id", "submission"} <= op.keys()
    return False


async def _apply(client, op: dict) -> None:
    kind = op["op"]
    if kind == "star":
        await client.set_star(op["item_id"], op["kind"], op["star"])
    elif kind == "rate":
        await client.set_rating(op["song_id"], op["rating"])
    elif kind == "scrobble":
        await client.scrobble(op["song_id"], op["submission"])
