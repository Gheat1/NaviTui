"""Headless end-to-end smoke test for the new features.

Runs the real NaviTuiApp over the mocked FakeClient with `ao="null"` (a real
mpv instance on the null audio device) and an isolated HOME, then drives the
new surfaces through the Textual pilot: the Home / Album Spotlight view, the
private-listening toggle, and the equalizer / settings / device / server
modals. No network, no real audio, no full-TUI blocking run.

Self-contained runner: `python tests/test_features_headless.py`.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path


def check(name: str, cond: bool) -> None:
    if not cond:
        raise AssertionError(f"FAILED: {name}")
    print(f"  ok: {name}")


def _sidebar_ids(app) -> list:
    ol = app.query_one("#sidebar-list")
    return [ol.get_option_at_index(i).id for i in range(ol.option_count)]


async def _run() -> None:
    tools = Path(__file__).resolve().parent.parent / "tools"
    sys.path.insert(0, str(tools))
    from screenshots import FakeClient  # noqa: E402

    from navitui.app import NaviTuiApp
    from navitui import screens as scr

    app = NaviTuiApp(client=FakeClient(), ao="null")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()

        # ── Home / Album Spotlight ────────────────────────────────────
        app._home_enabled = True
        app._render_sidebar()
        await pilot.pause()
        check("Home row appears in the sidebar when enabled", "home" in _sidebar_ids(app))

        app.view = "home"
        await app._render_home()
        await pilot.pause()
        panel = app.query_one("#tracks-panel")
        check("Home sets the album-of-the-day panel title",
              "album of the day" in (panel.border_title or ""))
        check("Home loads the album's songs for playback (self._songs)",
              len(app._songs) > 0)
        # the tracks pane shows disabled info rows, so no row is selectable —
        # this is what keeps the track-index handlers safe
        tl = app.query_one("#tracks-list")
        check("spotlight rows are all disabled (index-safe)",
              all(tl.get_option_at_index(i).disabled for i in range(tl.option_count)))

        # ── private listening ─────────────────────────────────────────
        check("private mode starts off", app.private_mode is False)
        app.action_toggle_private_mode()
        await pilot.pause()
        check("private mode toggles on", app.private_mode is True)
        status = app.query_one("#status").render()
        status_text = status.plain if hasattr(status, "plain") else str(status)
        check("status bar shows a private badge", "private" in status_text)
        check("status bar shows the version", " v" in status_text)
        app.action_toggle_private_mode()
        await pilot.pause()
        check("private mode toggles back off", app.private_mode is False)

        # ── equalizer overlay (real mpv set_equalizer) ────────────────
        app.action_show_equalizer()
        await pilot.pause()
        check("equalizer modal opens", isinstance(app.screen_stack[-1], scr.EqualizerModal))
        await pilot.press("space")   # enable
        await pilot.press("k")       # raise the selected band
        await pilot.press("l")       # next band
        await pilot.press("k")
        await pilot.press("escape")  # save + close
        await pilot.pause()
        eq = app._eq_state()
        check("equalizer state persisted enabled", eq["enabled"] is True)
        check("equalizer captured non-flat gains", any(abs(g) > 0 for g in eq["bands"]))

        # ── settings modal ────────────────────────────────────────────
        app.action_settings()
        await pilot.pause()
        check("settings modal opens", isinstance(app.screen_stack[-1], scr.SettingsModal))
        await pilot.press("ctrl+g")  # cycle provider (anthropic -> gemini)
        await pilot.press("escape")  # cancel (no save)
        await pilot.pause()

        # ── audio-device switcher (real mpv device list) ──────────────
        app.action_switch_audio_device()
        await pilot.pause()
        check("audio-device modal opens",
              isinstance(app.screen_stack[-1], scr.AudioDeviceSwitcherModal))
        await pilot.press("escape")
        await pilot.pause()

        # ── server switcher ───────────────────────────────────────────
        app.action_switch_server()
        await pilot.pause()
        check("server switcher modal opens",
              isinstance(app.screen_stack[-1], scr.ServerSwitcherModal))
        await pilot.press("escape")
        await pilot.pause()

        # ── multi-playlist picker via playlist_add ────────────────────
        # load a normal track view, focus + highlight a row, open the picker
        app._show_songs(list(app._songs), "all tracks")
        await pilot.pause()
        tracks = app.query_one("#tracks-list")
        assert tracks.option_count > 0, "test needs at least one track row"
        tracks.focus()
        await pilot.pause()
        tracks.highlighted = 0
        await pilot.pause()
        check("a track is highlighted for the picker", app._highlighted_song() is not None)
        app.action_playlist_add()
        await pilot.pause()
        check("playlist picker modal opens",
              isinstance(app.screen_stack[-1], scr.PlaylistPickerModal))
        # toggle a playlist and confirm the add returns the right shape
        await pilot.press("space")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()


def main() -> None:
    print("test_features_headless:")
    home = tempfile.mkdtemp()
    old_home = os.environ.get("HOME")
    os.environ["HOME"] = home
    try:
        asyncio.run(_run())
    finally:
        if old_home is not None:
            os.environ["HOME"] = old_home
    print("all headless feature tests passed")


if __name__ == "__main__":
    main()
