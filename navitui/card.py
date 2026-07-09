"""Now-playing card → shareable SVG.

A compact Rich panel of the current song (title / artist / album, a themed
progress bar, elapsed·total times) rendered to an SVG via a recording
`Console`. Palette is read at render time so every kit theme — including the
`system` ANSI theme, which `anim` degrades to flat styles — comes out right.

Real pixel cover art can't embed in an SVG built from cell text, so the card
leads with a nerd-font note glyph instead: tasteful, theme-colored, no data.
"""

from __future__ import annotations

import re
from pathlib import Path

from rich.align import Align
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ricekit import icons, palette

from navitui import anim
from navitui.models import Song

NOTE_GLYPH = "\uf001"  # nf-fa-music


def _slug(text: str) -> str:
    """A filesystem-safe stem from a title/artist."""
    slug = re.sub(r"[^\w-]+", "-", text.strip().lower()).strip("-")
    return slug or "track"


def render_card(song: Song, position: float, duration: float) -> Table:
    """The card body: header glyph, song metadata and a themed progress line,
    as a centered Rich grid. Split out from `export_svg` so the styling can be
    checked without touching a Console."""
    total = duration or song.duration or 0
    frac = position / total if total > 0 else 0.0

    title = Text(song.title, style=f"bold {palette.text}")
    if song.starred:
        title.append(f"  {icons.STAR}", style=palette.yellow)

    bar = anim.smooth_bar(frac, 28)
    bar.append(f"  {anim.fmt_time(position)} / {anim.fmt_time(total)}", style=palette.dim)

    stack = Table.grid()
    stack.add_column(justify="center")
    stack.add_row(Text(f"{NOTE_GLYPH}  now playing", style=palette.lav))
    stack.add_row(Text(""))
    stack.add_row(title)
    stack.add_row(Text(song.artist, style=palette.mauve))
    if song.album:
        stack.add_row(Text(song.album, style=palette.dim))
    stack.add_row(Text(""))
    stack.add_row(bar)
    return stack


def export_svg(song: Song, position: float, duration: float, path: Path) -> Path:
    """Render the card and write it to `path` as an SVG. Returns `path`."""
    card = Panel(
        Align.center(render_card(song, position, duration)),
        border_style=palette.blue,
        padding=(1, 3),
        expand=False,
        title="NaviTui",
        title_align="left",
    )
    console = Console(record=True, width=48)
    console.print(card)
    path.parent.mkdir(parents=True, exist_ok=True)
    console.save_svg(str(path), title=f"{song.title} — {song.artist}")
    return path


def export_path(song: Song, cache_dir: Path) -> Path:
    """Where a card for `song` is written: `<cache>/cards/<artist>-<title>.svg`."""
    stem = f"{_slug(song.artist)}-{_slug(song.title)}"
    return cache_dir / "cards" / f"{stem}.svg"
