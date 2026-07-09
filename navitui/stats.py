"""Local listening stats — a mini "wrapped", entirely offline.

Every confirmed play (the same moment NaviTui submits a scrobble to the
server — half the track, or 4 minutes, whichever comes first) appends one
line to a JSONL log in the AppDirs cache. Writes are a single cheap append
so logging never blocks the UI; a truncated/garbled last line just gets
skipped on read, so a crash mid-write can't corrupt the history.

The aggregations are pure functions over the parsed records: totals, top
tracks / artists (all-time and last 7 days), plays-per-day for a little
activity sparkline, and the current day streak. Nothing here touches the UI
or the network — the modal in `screens.py` renders whatever these return.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

# one JSONL line per confirmed play; append-only, newline-delimited
LOG_NAME = "plays.jsonl"

# nerd-font icons ricekit's curated set doesn't ship — written as \uXXXX
# escapes (raw PUA glyphs don't survive patch tooling) so the stats modal
# gets its own chart/fire/note without reaching into the sibling ricekit repo
ICON_CHART = "\uf080"   # bar chart
ICON_FIRE = "\uf06d"    # streak flame
ICON_MUSIC = "\uf001"   # music note


@dataclass
class Play:
    """One confirmed play, as stored on disk."""

    song_id: str
    title: str
    artist: str
    ts: float  # unix timestamp

    def to_line(self) -> str:
        return json.dumps(
            {"id": self.song_id, "title": self.title, "artist": self.artist, "ts": self.ts}
        )

    @classmethod
    def from_obj(cls, d: dict) -> "Play":
        return cls(
            song_id=str(d.get("id", "")),
            title=str(d.get("title", "")),
            artist=str(d.get("artist", "")),
            ts=float(d.get("ts", 0.0)),
        )


class StatsStore:
    """Append-only play log under the cache dir. Cheap, crash-safe writes."""

    def __init__(self, cache_dir: Path) -> None:
        self.path = Path(cache_dir) / LOG_NAME

    def log_play(
        self, song_id: str, title: str, artist: str, ts: float | None = None
    ) -> None:
        """Append one play record. Never raises — a stats write must never
        take down playback. `ts` defaults to now (pass a fixed value in tests)."""
        play = Play(song_id, title, artist, time.time() if ts is None else ts)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(play.to_line() + "\n")
        except OSError:
            pass

    def load(self) -> list[Play]:
        """Read all play records, skipping any unparseable (e.g. a torn last
        line from a crash mid-write). Returns [] when there's no history."""
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError:
            return []
        plays: list[Play] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                plays.append(Play.from_obj(json.loads(line)))
            except (ValueError, TypeError):
                continue  # torn / garbled line — skip it, keep the rest
        return plays


# ── pure aggregations over a list of plays ────────────────────────────────
# `now` is injectable so tests can pin "today"; production passes time.time().


def _day(ts: float) -> date:
    return datetime.fromtimestamp(ts).date()


def total_plays(plays: list[Play]) -> int:
    return len(plays)


def since(plays: list[Play], now: float, days: int) -> list[Play]:
    """Plays within the last `days` (inclusive of a full 7-day window)."""
    cutoff = now - days * 86400
    return [p for p in plays if p.ts >= cutoff]


def top_tracks(plays: list[Play], limit: int = 5) -> list[tuple[str, str, int]]:
    """Most-played (title, artist, count), highest first. Keyed on
    title+artist so the same song counts together across ids."""
    counts: Counter[tuple[str, str]] = Counter()
    for p in plays:
        counts[(p.title, p.artist)] += 1
    return [(title, artist, n) for (title, artist), n in counts.most_common(limit)]


def top_artists(plays: list[Play], limit: int = 5) -> list[tuple[str, int]]:
    """Most-played (artist, count), highest first. Blank artists are ignored."""
    counts: Counter[str] = Counter()
    for p in plays:
        if p.artist:
            counts[p.artist] += 1
    return counts.most_common(limit)


def plays_per_day(plays: list[Play], now: float, days: int = 14) -> list[int]:
    """Play counts for the last `days` days, oldest → newest (today last).
    Drives the activity sparkline; length is always `days`."""
    today = _day(now)
    buckets = {i: 0 for i in range(days)}
    for p in plays:
        delta = (today - _day(p.ts)).days
        if 0 <= delta < days:
            buckets[days - 1 - delta] += 1
    return [buckets[i] for i in range(days)]


def current_streak(plays: list[Play], now: float) -> int:
    """Consecutive days with at least one play, counting back from today (or
    yesterday if nothing yet today). 0 when the chain is already broken."""
    if not plays:
        return 0
    listened = {_day(p.ts) for p in plays}
    today = _day(now)
    from datetime import timedelta

    if today not in listened:
        # a fresh day with no plays yet shouldn't zero an active streak
        if (today - timedelta(days=1)) not in listened:
            return 0
        today = today - timedelta(days=1)
    streak = 0
    cursor = today
    while cursor in listened:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def sparkline(counts: list[int]) -> str:
    """Render play counts as a unicode block sparkline. Scales to the max in
    the window; all-zero (or empty) stays flat on the baseline block."""
    blocks = "▁▂▃▄▅▆▇█"  # ▁▂▃▄▅▆▇█
    if not counts:
        return ""
    peak = max(counts)
    if peak <= 0:
        return blocks[0] * len(counts)
    out = []
    for c in counts:
        if c <= 0:
            out.append(blocks[0])
        else:
            idx = 1 + round((c / peak) * (len(blocks) - 2))
            out.append(blocks[min(idx, len(blocks) - 1)])
    return "".join(out)


@dataclass
class Summary:
    """Everything the stats modal needs, computed once on open."""

    total: int
    week_total: int
    top_tracks_week: list[tuple[str, str, int]]
    top_tracks_all: list[tuple[str, str, int]]
    top_artists_week: list[tuple[str, int]]
    top_artists_all: list[tuple[str, int]]
    per_day: list[int]
    streak: int
    days_window: int

    @property
    def empty(self) -> bool:
        return self.total == 0


def summarize(plays: list[Play], now: float, days_window: int = 14) -> Summary:
    """Fold the whole log into one Summary. Pure; `now` is injectable."""
    week = since(plays, now, 7)
    return Summary(
        total=total_plays(plays),
        week_total=len(week),
        top_tracks_week=top_tracks(week),
        top_tracks_all=top_tracks(plays),
        top_artists_week=top_artists(week),
        top_artists_all=top_artists(plays),
        per_day=plays_per_day(plays, now, days_window),
        streak=current_streak(plays, now),
        days_window=days_window,
    )
