"""The play queue — ordering, shuffle, and repeat live here, not in the UI.

The queue's `songs` list IS the play order (what the queue panel shows is
exactly what will play). Toggling shuffle on keeps the current track first
and shuffles the rest; toggling it off restores the original order with the
current track still current.
"""

from __future__ import annotations

import random
from enum import Enum

from navitui.models import Song


class Repeat(Enum):
    OFF = "off"
    ALL = "all"
    ONE = "one"

    def next(self) -> "Repeat":
        order = [Repeat.OFF, Repeat.ALL, Repeat.ONE]
        return order[(order.index(self) + 1) % len(order)]


class PlayQueue:
    def __init__(self) -> None:
        self.songs: list[Song] = []
        self.index: int = -1
        self.repeat: Repeat = Repeat.OFF
        self.shuffle: bool = False
        self._original: list[Song] = []

    # ── content ───────────────────────────────────────────────────────
    @property
    def current(self) -> Song | None:
        if 0 <= self.index < len(self.songs):
            return self.songs[self.index]
        return None

    def set_songs(self, songs: list[Song], start: int = 0) -> Song | None:
        self._original = list(songs)
        self.songs = list(songs)
        self.index = start if 0 <= start < len(songs) else (0 if songs else -1)
        if self.shuffle and self.songs:
            self._shuffle_rest()
        return self.current

    def add(self, songs: list[Song]) -> None:
        self.songs.extend(songs)
        self._original.extend(songs)
        if self.index < 0 and self.songs:
            self.index = 0

    def add_next(self, songs: list[Song]) -> None:
        at = self.index + 1 if self.index >= 0 else 0
        self.songs[at:at] = songs
        self._original.extend(songs)

    def remove(self, i: int) -> Song | None:
        if not (0 <= i < len(self.songs)):
            return None
        song = self.songs.pop(i)
        try:
            self._original.remove(song)
        except ValueError:
            pass
        if i < self.index:
            self.index -= 1
        elif i == self.index:
            self.index = min(self.index, len(self.songs) - 1)
        return song

    def move(self, i: int, delta: int) -> int | None:
        """Swap song i with its neighbour; returns the new position. The
        current track can be moved too — `index` follows it."""
        j = i + delta
        if not (0 <= i < len(self.songs) and 0 <= j < len(self.songs)):
            return None
        self.songs[i], self.songs[j] = self.songs[j], self.songs[i]
        if self.index == i:
            self.index = j
        elif self.index == j:
            self.index = i
        return j

    def clear(self) -> None:
        self.songs = []
        self._original = []
        self.index = -1

    # ── movement ──────────────────────────────────────────────────────
    def advance(self, natural: bool = True) -> Song | None:
        """Next track. `natural=True` means the previous one finished on
        its own, which is the only case where repeat-one repeats."""
        if not self.songs:
            return None
        if natural and self.repeat is Repeat.ONE:
            return self.current
        if self.index + 1 < len(self.songs):
            self.index += 1
            return self.current
        if self.repeat is not Repeat.OFF or not natural:
            self.index = 0
            return self.current
        return None  # queue ran out

    def prev(self) -> Song | None:
        if not self.songs:
            return None
        self.index = max(0, self.index - 1)
        return self.current

    def jump(self, i: int) -> Song | None:
        if 0 <= i < len(self.songs):
            self.index = i
        return self.current

    # ── modes ─────────────────────────────────────────────────────────
    def toggle_shuffle(self) -> bool:
        self.shuffle = not self.shuffle
        current = self.current
        if self.shuffle:
            self._shuffle_rest()
        else:
            self.songs = list(self._original)
            self.index = self.songs.index(current) if current in self.songs else -1
        return self.shuffle

    def _shuffle_rest(self) -> None:
        """Current track stays put; everything after plays in random order."""
        current = self.current
        rest = [s for i, s in enumerate(self.songs) if i != self.index]
        random.shuffle(rest)
        if current is not None:
            self.songs = [current] + rest
            self.index = 0
        else:
            self.songs = rest

    def cycle_repeat(self) -> Repeat:
        self.repeat = self.repeat.next()
        return self.repeat

    # ── persistence ───────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "songs": [s.to_dict() for s in self.songs],
            "original": [s.to_dict() for s in self._original],
            "index": self.index,
            "repeat": self.repeat.value,
            "shuffle": self.shuffle,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PlayQueue":
        q = cls()
        try:
            q.songs = [Song.from_dict(s) for s in d.get("songs", [])]
            q._original = [Song.from_dict(s) for s in d.get("original", [])] or list(q.songs)
            q.index = int(d.get("index", -1))
            q.repeat = Repeat(d.get("repeat", "off"))
            q.shuffle = bool(d.get("shuffle", False))
            if not (-1 <= q.index < len(q.songs)):
                q.index = -1
        except Exception:
            return cls()
        return q
