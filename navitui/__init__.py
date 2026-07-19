"""NaviTui — a fast, animated terminal player for Navidrome.

Built on Textual and ricekit; playback via libmpv; cover art over the
kitty/sixel graphics protocols with a unicode fallback.
"""

from __future__ import annotations

__version__ = "0.4.0"

# Run terminal-protocol detection before anything imports navitui.art (which
# pulls in textual_image). This forces the kitty graphics protocol on
# Ghostty/Kitty and defuses textual_image's blocking sixel/tgp probe, which
# can otherwise hang startup for seconds. Import for side effects only.
from navitui import terminal_probe as terminal_probe  # noqa: E402,F401


def main() -> None:
    from navitui.app import main as run

    run()
