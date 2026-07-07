"""NaviTui — a fast, animated terminal player for Navidrome.

Built on Textual and ricekit; playback via libmpv; cover art over the
kitty/sixel graphics protocols with a unicode fallback.
"""

from __future__ import annotations

__version__ = "0.2.0"


def main() -> None:
    from navitui.app import main as run

    run()
