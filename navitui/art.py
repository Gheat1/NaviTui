"""Cover art — real images where the terminal can, graceful glyphs where it
can't.

`textual-image` picks the best protocol at runtime (kitty graphics → sixel →
truecolor half-cells → plain unicode). Override with NAVITUI_ART=
auto|tgp|sixel|halfcell|unicode|off. Everything is wrapped defensively: art
must never be the reason the player doesn't start (headless terminals,
multiplexers and ssh all get the fallback or the placeholder).
"""

from __future__ import annotations

import os
from pathlib import Path

from rich.text import Text
from textual.containers import Vertical
from textual.widgets import Static

from ricekit import palette
from ricekit.widgets import pop_in


def _image_class():
    kind = os.environ.get("NAVITUI_ART", "auto").lower()
    if kind == "off":
        return None
    try:
        from textual_image import widget as tiw

        return {
            "auto": tiw.Image,  # auto-detected best protocol
            "tgp": tiw.TGPImage,
            "sixel": tiw.SixelImage,
            "halfcell": tiw.HalfcellImage,
            "unicode": tiw.UnicodeImage,
        }.get(kind, tiw.Image)
    except Exception:
        return None


class CoverArt(Vertical):
    """The album-art panel. `show(path)` swaps the image with a fade;
    `placeholder()` draws a big dim note instead of an error."""

    DEFAULT_CSS = """
    CoverArt { align: center middle; }
    CoverArt > .cover-image { width: 100%; height: 100%; }
    CoverArt > #cover-placeholder {
        width: 100%; height: 100%;
        content-align: center middle;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._current: str | None = None
        self._cls = _image_class()

    def compose(self):
        yield Static(self._placeholder_text(), id="cover-placeholder")

    def _placeholder_text(self) -> Text:
        t = Text(justify="center")
        t.append("♪\n\n", style=f"bold {palette.vfaint}")
        t.append("no cover", style=palette.faint)
        return t

    def placeholder(self) -> None:
        self._current = None
        self._swap(Static(self._placeholder_text(), id="cover-placeholder"))

    def show(self, path: Path, key: str) -> None:
        """`key` identifies the art so re-selecting the same album is free."""
        if key == self._current:
            return
        self._current = key
        if self._cls is None:
            return  # art disabled/unavailable; keep the placeholder
        try:
            image = self._cls(str(path), classes="cover-image")
        except Exception:
            self.placeholder()
            return
        self._swap(image)

    def _swap(self, widget) -> None:
        async def do() -> None:
            await self.remove_children()
            await self.mount(widget)
            pop_in(widget, duration=0.25)

        self.app.call_next(do)
