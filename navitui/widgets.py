"""NaviTui's animated widgets.

One shared 8fps heartbeat in the app calls `.tick()` on each of these; every
widget only repaints its own few cells, so the constant motion costs almost
nothing. Colors are read from `ricekit.palette` at render time so a theme
switch restyles every animation live.
"""

from __future__ import annotations

import math

from rich.text import Text
from textual.widgets import Static

from ricekit import icons, palette

from navitui import anim
from navitui.models import Song
from navitui.playqueue import Repeat

SHUFFLE_ICON = "\uf074"  # nf-fa-random
REPEAT_ICON = "\uf01e"  # nf-fa-repeat
PLAY_GLYPH = "\uf04b"  # nf-fa-play
PAUSE_GLYPH = "\uf04c"  # nf-fa-pause


class Logo(Static):
    """The NaviTui wordmark with a constant shimmer sweeping across it."""

    DEFAULT_CSS = """
    Logo { width: auto; height: 1; padding: 0 1; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._phase = 0.0

    def tick(self) -> None:
        self._phase += 0.55
        self.update(self.logo_text())

    def logo_text(self) -> Text:
        t = Text()
        t.append(anim.note(int(self._phase)) + " ", style=palette.mauve)
        t.append_text(anim.shimmer("NaviTui", self._phase, palette.mauve, palette.text))
        return t


class Visualizer(Static):
    """Standalone EQ bars (used in the onboarding screen for flair)."""

    DEFAULT_CSS = """
    Visualizer { width: auto; height: 1; }
    """

    def __init__(self, bars: int = 5, **kwargs) -> None:
        super().__init__(**kwargs)
        self.model = anim.VizModel(bars)

    def tick(self) -> None:
        self.model.tick()
        self.update(self.model.render())


class NowPlaying(Static):
    """The two-line transport: viz + title marquee + star on top, smooth
    progress bar with times, volume gauge and mode toggles below.

    Click the bar to seek, click the gauge to set volume, click the
    shuffle/repeat glyphs to toggle them (they route through app actions).
    """

    DEFAULT_CSS = """
    NowPlaying {
        height: 4;
        border: round $kit-border;
        padding: 0 1;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.song: Song | None = None
        self.playing = False
        self.position = 0.0
        self.duration = 0.0
        self.volume = 100
        self.muted = False
        self.shuffle = False
        self.repeat = Repeat.OFF
        self.viz = anim.VizModel(5, seed=7)
        self._tick = 0
        self._title_flash = 0  # ticks of brightness after a track change
        self._vol_flash = 0
        self._bar_span: tuple[int, int] = (0, 0)  # x range of the seek bar
        self._gauge_span: tuple[int, int] = (0, 0)
        self._mode_spans: dict[str, tuple[int, int]] = {}
        self.border_title = "now playing"

    # ── state from the app ────────────────────────────────────────────
    def set_song(self, song: Song | None) -> None:
        if song is not None and (self.song is None or song.id != self.song.id):
            self._title_flash = 12
        self.song = song
        if song is None:
            self.position = 0.0
            self.duration = 0.0

    def set_progress(self, position: float, duration: float) -> None:
        self.position = position
        if duration > 0:
            self.duration = duration

    def set_playing(self, playing: bool) -> None:
        self.playing = playing

    def flash_volume(self) -> None:
        self._vol_flash = 10

    def tick(self) -> None:
        self._tick += 1
        self.viz.energy = 1.0 if self.playing else 0.0
        self.viz.tick()
        if self._title_flash > 0:
            self._title_flash -= 1
        if self._vol_flash > 0:
            self._vol_flash -= 1
        self.update(self._render_lines())

    # ── drawing ───────────────────────────────────────────────────────
    def _render_lines(self) -> Text:
        width = max(20, self.content_size.width)
        return Text("\n").join([self._line_top(width), self._line_bottom(width)])

    def _line_top(self, width: int) -> Text:
        line = Text()
        line.append_text(self.viz.render())
        line.append("  ")
        if self.song is None:
            line.append("nothing playing", style=palette.dim)
            line.append("  ·  press ", style=palette.vfaint)
            line.append("enter", style=palette.dim)
            line.append(" on a track", style=palette.vfaint)
            return line
        star = f" {icons.STAR}" if self.song.starred else ""
        state = PLAY_GLYPH if self.playing else PAUSE_GLYPH
        line.append(f"{state} ", style=palette.green if self.playing else palette.peach)
        # brighten the title briefly on track change, then settle
        flash = self._title_flash / 12
        title_color = anim.blend(palette.text, "#ffffff", 0.7 * flash)
        room = width - line.cell_len - len(star) - 1
        body = f"{self.song.title}  —  {self.song.artist} · {self.song.album}"
        line.append(anim.marquee(body, max(8, room), self._tick // 2), style=f"bold {title_color}")
        if star:
            line.append(star, style=palette.yellow)
        return line

    def _line_bottom(self, width: int) -> Text:
        elapsed = anim.fmt_time(self.position)
        total = anim.fmt_time(self.duration or (self.song.duration if self.song else 0))
        times = f" {elapsed} / {total} "

        # right side: volume + modes
        right = Text("  ")
        gauge_start = None
        vol_frac = 0.0 if self.muted else self.volume / 100
        right.append("vol ", style=palette.vfaint)
        gauge_start = right.cell_len
        gauge = anim.mini_gauge(vol_frac, 6)
        if self._vol_flash > 0 and anim.can_blend():
            gauge.stylize(anim.blend(palette.lav, "#ffffff", self._vol_flash / 14))
        right.append_text(gauge)
        gauge_end = right.cell_len
        vol_label = "mut" if self.muted else f"{self.volume:>3d}"
        right.append(f" {vol_label}", style=palette.red if self.muted else palette.dim)
        right.append("  ")
        shuf_start = right.cell_len
        right.append(
            f"{SHUFFLE_ICON} ",
            style=palette.peach if self.shuffle else palette.vfaint,
        )
        shuf_end = right.cell_len
        rep_start = right.cell_len
        rep_style = palette.peach if self.repeat is not Repeat.OFF else palette.vfaint
        right.append(REPEAT_ICON, style=rep_style)
        if self.repeat is Repeat.ONE:
            right.append("¹", style=palette.peach)
        rep_end = right.cell_len

        bar_width = max(4, width - len(times) - right.cell_len)
        frac = self.position / self.duration if self.duration > 0 else 0.0
        pulse = (math.sin(self._tick * 0.55) + 1) / 2 if self.playing else 0.0
        line = Text()
        line.append_text(anim.smooth_bar(frac, bar_width, head_pulse=pulse))
        line.append(times, style=palette.dim)
        base = line.cell_len
        line.append_text(right)

        # remember hit-boxes for the mouse (content coordinates, line y=1)
        self._bar_span = (0, bar_width)
        self._gauge_span = (base + gauge_start, base + gauge_end)
        self._mode_spans = {
            "shuffle": (base + shuf_start, base + shuf_end),
            "repeat": (base + rep_start, base + rep_end),
        }
        return line

    # ── mouse ─────────────────────────────────────────────────────────
    def on_click(self, event) -> None:
        content = event.get_content_offset(self)
        if content is None:
            return
        x, y = content
        if y != 1:
            return
        b0, b1 = self._bar_span
        if b0 <= x < b1 and b1 > b0:
            self.app.seek_fraction((x - b0 + 0.5) / (b1 - b0))
            return
        g0, g1 = self._gauge_span
        if g0 <= x < g1:
            self.app.set_volume_fraction((x - g0 + 0.5) / (g1 - g0))
            return
        for name, (m0, m1) in self._mode_spans.items():
            if m0 <= x < m1:
                if name == "shuffle":
                    self.app.action_toggle_shuffle()
                else:
                    self.app.action_cycle_repeat()
                return
