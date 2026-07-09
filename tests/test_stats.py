"""Stdlib-only tests for local listening stats (#18).

The repo ships no test framework, so this is a self-contained runner:
`python tests/test_stats.py`. It covers the append-only play store and every
pure aggregation with FIXED timestamps (never wall-clock), then headlessly
opens the stats modal over the mocked client with `ao="null"` and an isolated
`$HOME` so nothing touches a real config, cache, or audio device.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from navitui import stats as st

# a fixed "now" so day-bucketing and streaks are deterministic
NOW = datetime(2026, 7, 8, 12, 0, 0).timestamp()
DAY = 86400.0


def _at(days_ago: float, hour: int = 12) -> float:
    """A timestamp `days_ago` days before NOW's date, at `hour`:00 local."""
    base = datetime(2026, 7, 8, hour, 0, 0) - timedelta(days=days_ago)
    return base.timestamp()


PASSED = 0
FAILED = 0


def check(name: str, cond: bool) -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  ok   {name}")
    else:
        FAILED += 1
        print(f"  FAIL {name}")


# ── store: append + crash-safe read ───────────────────────────────────────
def test_store_roundtrip() -> None:
    with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as d:
        store = st.StatsStore(Path(d))
        check("empty log reads []", store.load() == [])
        store.log_play("s1", "Song One", "Artist A", ts=NOW)
        store.log_play("s2", "Song Two", "Artist B", ts=NOW)
        plays = store.load()
        check("two appended plays read back", len(plays) == 2)
        check("record fields preserved",
              plays[0].song_id == "s1" and plays[0].title == "Song One"
              and plays[0].artist == "Artist A" and plays[0].ts == NOW)

        # simulate a crash mid-write: a torn/garbled trailing line
        with store.path.open("a", encoding="utf-8") as fh:
            fh.write('{"id": "s3", "title": "Half')  # no newline, invalid JSON
        plays = store.load()
        check("torn last line skipped, rest intact", len(plays) == 2)

        # log never raises even if the dir is unwritable
        bad = st.StatsStore(Path("/proc/nonexistent-dir-xyz/nope"))
        bad.log_play("x", "y", "z", ts=NOW)  # must not raise
        check("log_play swallows write errors", bad.load() == [])


# ── aggregations (fixed timestamps) ───────────────────────────────────────
def sample_plays() -> list[st.Play]:
    # today: 3x "Alpha"/Aria, 1x "Beta"/Bram
    # yesterday: 2x "Alpha"/Aria, 1x "Gamma"/Aria
    # 3 days ago: 1x "Delta"/Cleo
    # 10 days ago (outside 7d window): 5x "Old"/Zed
    plays: list[st.Play] = []

    def add(n, sid, title, artist, days):
        for _ in range(n):
            plays.append(st.Play(sid, title, artist, _at(days)))

    add(3, "a", "Alpha", "Aria", 0)
    add(1, "b", "Beta", "Bram", 0)
    add(2, "a", "Alpha", "Aria", 1)
    add(1, "g", "Gamma", "Aria", 1)
    add(1, "d", "Delta", "Cleo", 3)
    add(5, "o", "Old", "Zed", 10)
    return plays


def test_aggregations() -> None:
    plays = sample_plays()
    check("total_plays counts everything", st.total_plays(plays) == 13)

    week = st.since(plays, NOW, 7)
    check("since(7d) excludes the 10-day-old block", len(week) == 8)

    top_all = st.top_tracks(plays)
    check("top track all-time is Alpha x5", top_all[0] == ("Alpha", "Aria", 5))
    check("Old x5 present all-time", ("Old", "Zed", 5) in top_all)

    top_week = st.top_tracks(week)
    check("top track this week is Alpha x5", top_week[0] == ("Alpha", "Aria", 5))
    check("Old absent from this-week tracks",
          all(t[0] != "Old" for t in top_week))

    art_all = st.top_artists(plays)
    # Aria: 3+2+1 = 6, Zed: 5, Bram: 1, Cleo: 1
    check("top artist all-time is Aria x6", art_all[0] == ("Aria", 6))
    art_week = st.top_artists(week)
    check("Zed absent from this-week artists",
          all(a[0] != "Zed" for a in art_week))

    per_day = st.plays_per_day(plays, NOW, days=14)
    check("per_day length == window", len(per_day) == 14)
    check("today (last bucket) has 4 plays", per_day[-1] == 4)
    check("yesterday bucket has 3 plays", per_day[-2] == 3)
    check("3-days-ago bucket has 1 play", per_day[-4] == 1)
    check("10-days-ago bucket has 5 plays", per_day[-11] == 5)


def test_streak() -> None:
    # today, yesterday, 3-days-ago present -> streak breaks at the gap: 2
    plays = sample_plays()
    check("streak counts consecutive days back from today == 2",
          st.current_streak(plays, NOW) == 2)

    # only yesterday's plays: a fresh day with nothing yet keeps streak alive
    y = [st.Play("a", "A", "X", _at(1)), st.Play("b", "B", "X", _at(2))]
    check("streak survives a still-empty today == 2",
          st.current_streak(y, NOW) == 2)

    # gap: last play 2 days ago -> broken
    old = [st.Play("a", "A", "X", _at(2))]
    check("broken streak == 0", st.current_streak(old, NOW) == 0)

    check("no plays -> streak 0", st.current_streak([], NOW) == 0)


def test_sparkline() -> None:
    check("empty sparkline is ''", st.sparkline([]) == "")
    flat = st.sparkline([0, 0, 0])
    check("all-zero stays on baseline block", set(flat) == {"▁"})
    line = st.sparkline([0, 1, 5])
    check("sparkline length matches input", len(line) == 3)
    check("peak maps to full block", line[-1] == "█")
    check("zero maps to baseline block", line[0] == "▁")


def test_summary() -> None:
    s = st.summarize(sample_plays(), NOW, days_window=14)
    check("summary total", s.total == 13)
    check("summary week total", s.week_total == 8)
    check("summary streak", s.streak == 2)
    check("summary top track this week", s.top_tracks_week[0][0] == "Alpha")
    check("summary not empty", s.empty is False)

    empty = st.summarize([], NOW)
    check("empty summary flagged", empty.empty is True)
    check("empty summary zero streak", empty.streak == 0)


# ── headless: open the stats modal over the mocked client ─────────────────
def test_modal_headless() -> None:
    """Push StatsModal (populated + empty) with ao='null' and an isolated
    HOME, driving the mocked FakeClient. No blocking full-TUI run."""
    import sys

    tools = Path(__file__).resolve().parent.parent / "tools"
    sys.path.insert(0, str(tools))
    from screenshots import FakeClient  # noqa: E402  (mocked client)

    from navitui.app import NaviTuiApp
    from navitui.screens import StatsModal

    async def run() -> None:
        app = NaviTuiApp(client=FakeClient(), ao="null")
        async with app.run_test(size=(120, 40)) as pilot:
            # seed a couple of confirmed plays through the real store
            app.stats.log_play("s1", "Seeded Track", "Seeded Artist", ts=NOW)
            app.stats.log_play("s1", "Seeded Track", "Seeded Artist", ts=NOW)
            app.action_stats()
            await pilot.pause()
            top = app.screen_stack[-1]
            check("stats modal is on top", isinstance(top, StatsModal))
            check("modal summary saw the seeded plays",
                  top._summary.total == 2)
            await pilot.press("escape")
            await pilot.pause()

            # empty state: point the store at a clean dir, reopen
            with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as d:
                app.stats = st.StatsStore(Path(d))
                app.action_stats()
                await pilot.pause()
                top = app.screen_stack[-1]
                check("empty-state modal opens", isinstance(top, StatsModal))
                check("empty-state summary flagged empty", top._summary.empty)
                await pilot.press("escape")
                await pilot.pause()

    home = tempfile.mkdtemp()
    old_home = os.environ.get("HOME")
    os.environ["HOME"] = home
    try:
        asyncio.run(run())
    finally:
        if old_home is not None:
            os.environ["HOME"] = old_home


def main() -> None:
    print("stats store + aggregations")
    test_store_roundtrip()
    test_aggregations()
    test_streak()
    test_sparkline()
    test_summary()
    print("headless modal")
    test_modal_headless()
    print(f"\n{PASSED} passed, {FAILED} failed")
    raise SystemExit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
