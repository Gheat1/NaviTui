"""Drive the app into a photogenic state inside a REAL terminal.

    kitty -e .venv/bin/python tools/demo.py <state>

States: main | albums | search | void. Fake library, isolated HOME, silent
audio (ao=null) — but everything else is the real app in a real terminal,
so kitty renders genuine pixel-graphics cover art. Used by shots.sh for
Reddit/README pictures.
"""

from __future__ import annotations

import asyncio
import math
import os
import struct
import sys
import wave
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

from screenshots import FakeClient  # noqa: E402  (isolates HOME on import)

os.environ["NAVITUI_ART"] = "auto"  # real kitty graphics, not halfcell

from navitui.app import NaviTuiApp  # noqa: E402
from navitui.screens import SearchModal  # noqa: E402


def long_tone(path: Path) -> None:
    """~3.5 minutes of a soft chord so the transport shows real song math.
    Frequencies are multiples of 55Hz, so one second tiles seamlessly —
    generation is instant instead of millions of sin() calls."""
    rate = 22050
    seconds = 210
    one = bytearray()
    for i in range(rate):
        t = i / rate
        sample = 0.12 * (
            math.sin(2 * math.pi * 220 * t)
            + 0.6 * math.sin(2 * math.pi * 275 * t)
        )
        one += struct.pack("<h", int(sample * 32767))
    with wave.open(str(path), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(one) * seconds)


class DemoApp(NaviTuiApp):
    def __init__(self, state: str) -> None:
        client = FakeClient()
        long_tone(client._tone)
        super().__init__(client=client, ao="null")
        self._demo_state = state

    def on_mount(self) -> None:
        super().on_mount()
        self.run_worker(self._drive(), group="demo")

    async def _drive(self) -> None:
        state = self._demo_state
        await asyncio.sleep(1.5)  # sidebar + all-tracks view settle

        # start playback on track 3 of the all-tracks view, seek mid-song
        self._play_songs(self._songs, 2)
        await asyncio.sleep(2.0)  # mpv buffers, duration lands
        self.player.seek_to(0.44)
        self.query_one("#tracks-list").focus()

        # terminal query responses can leak a stray key during startup and
        # open a modal; make sure we're photographing the base screen
        while len(self.screen_stack) > 1:
            self.pop_screen()

        if state == "void":
            self.theme = "void"
        elif state == "playlist":
            sidebar = self.query_one("#sidebar-list")
            sidebar.focus()
            self._highlight_view(f"pl:{(await self.client.get_playlists())[0].id}")
            await asyncio.sleep(0.8)
        elif state == "search":
            self.push_screen(SearchModal())
            await asyncio.sleep(0.4)
            search_input = self.screen.query_one("#search-input")
            search_input.value = "light"


def main() -> None:
    state = sys.argv[1] if len(sys.argv) > 1 else "main"
    DemoApp(state).run()


if __name__ == "__main__":
    main()
