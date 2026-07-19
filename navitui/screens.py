"""Screens and modals: first-run onboarding and global search.

Onboarding follows the kit doctrine — never dump a new user into an empty
screen with an error toast. Credentials are validated live against the
server and only stored (chmod 600) once a ping succeeds.
"""

from __future__ import annotations

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from ricekit import icons, palette
from ricekit.widgets import NavList, pop_in

from navitui import anim, stats as statsmod
from navitui.api import SubsonicError, make_token, normalize_server
from navitui.models import SearchResults
from navitui.widgets import Logo, Visualizer


def settle_pop_in(screen, box_selector: str) -> None:
    """textual 8 sharp edge: `Widget.visual_style` caches the blended text
    background while an ancestor's opacity is still animating (the cache key
    ignores ancestor opacity), so text inside a pop_in'd box keeps a smudged
    background forever. Bust the cache once the fade has finished."""

    def bust() -> None:
        for widget in screen.query(f"{box_selector}, {box_selector} *"):
            widget._visual_style = None
            widget.refresh()

    screen.set_timer(0.25, bust)


class OnboardingScreen(Screen):
    """Server + credentials, validated live. Dismisses with the config dict."""

    BINDINGS = [Binding("escape", "quit_app", "quit", show=True)]

    DEFAULT_CSS = """
    OnboardingScreen { align: center middle; }
    OnboardingScreen #onboard-box {
        width: 58; height: auto;
        border: round $kit-border-focus;
        background: $kit-modal-bg;
        padding: 1 3;
    }
    OnboardingScreen #onboard-head { height: 1; margin-bottom: 1; }
    OnboardingScreen Visualizer { margin: 0 2 0 0; }
    OnboardingScreen Input {
        background: transparent;
        border: round $kit-border;
        margin-bottom: 0;
    }
    OnboardingScreen Input:focus { border: round $kit-border-focus; }
    OnboardingScreen #onboard-status { height: 2; padding: 0 1; }
    """

    def __init__(self, server: str = "", username: str = "") -> None:
        super().__init__()
        self._server = server
        self._username = username

    def compose(self) -> ComposeResult:
        with Vertical(id="onboard-box"):
            with Horizontal(id="onboard-head"):
                yield Visualizer(bars=4)
                yield Logo()
                yield Static(
                    Text("connect to your navidrome", style=palette.dim),
                )
            yield Input(
                value=self._server,
                placeholder="server · https://music.example.com",
                id="in-server",
            )
            yield Input(value=self._username, placeholder="username", id="in-user")
            yield Input(placeholder="password", password=True, id="in-pass")
            yield Static(self._hint(), id="onboard-status")

    def _hint(self) -> Text:
        t = Text()
        t.append("enter", style=palette.blue)
        t.append(" connect  ·  ", style=palette.vfaint)
        t.append("tab", style=palette.blue)
        t.append(" next field  ·  stored locally, chmod 600", style=palette.vfaint)
        return t

    def on_mount(self) -> None:
        pop_in(self.query_one("#onboard-box"))
        settle_pop_in(self, "#onboard-box")
        target = "#in-server" if not self._server else "#in-user"
        self.query_one(target, Input).focus()
        self.set_interval(1 / 8, self._tick)
        viz = self.query_one(Visualizer)
        viz.model.energy = 0.6

    def _tick(self) -> None:
        self.query_one(Logo).tick()
        self.query_one(Visualizer).tick()

    @on(Input.Submitted)
    def _submitted(self, event: Input.Submitted) -> None:
        order = ["in-server", "in-user", "in-pass"]
        values = {i: self.query_one(f"#{i}", Input).value.strip() for i in order}
        for field in order:
            if not values[field]:
                self.query_one(f"#{field}", Input).focus()
                return
        self._connect(values["in-server"], values["in-user"], values["in-pass"])

    def _status(self, text: Text) -> None:
        status = self.query_one("#onboard-status", Static)
        status.update(text)
        pop_in(status)

    @work(exclusive=True, group="onboard")
    async def _connect(self, server: str, username: str, password: str) -> None:
        import httpx

        from navitui.api import SubsonicClient

        server = normalize_server(server)
        token, salt = make_token(password)
        spin = Text()
        spin.append(f"{anim.spinner(0)} ", style=palette.blue)
        spin.append(f"pinging {server} …", style=palette.sub)
        self._status(spin)
        client = SubsonicClient(server, username, token, salt, art_dir=self.app.dirs.cache_dir / "art")
        try:
            body = await client.ping()
        except SubsonicError as e:
            fail = Text()
            fail.append(f"{icons.CROSS_CIRCLE} ", style=palette.red)
            fail.append(str(e), style=palette.red)
            self._status(fail)
            self.query_one("#in-pass", Input).focus()
            return
        except (httpx.HTTPError, OSError) as e:
            fail = Text()
            fail.append(f"{icons.CROSS_CIRCLE} ", style=palette.red)
            fail.append(f"can't reach server: {e}", style=palette.red)
            self._status(fail)
            return
        finally:
            await client.close()

        okay = Text()
        okay.append(f"{icons.CHECK_CIRCLE} ", style=palette.green)
        server_kind = body.get("type", "subsonic")
        okay.append(f"connected — {server_kind} {body.get('serverVersion', '')}", style=palette.green)
        self._status(okay)
        self.dismiss({"server": server, "username": username, "token": token, "salt": salt})

    def action_quit_app(self) -> None:
        self.app.exit()


class InputModal(ModalScreen):
    """One-line text prompt (e.g. a new playlist name). Dismisses with the
    entered string, or None on escape."""

    BINDINGS = [Binding("escape", "cancel", show=False)]

    DEFAULT_CSS = """
    InputModal { align: center middle; background: $kit-overlay; }
    InputModal #input-box {
        width: 52; height: auto;
        background: $kit-modal-bg; border: round $kit-border-focus; padding: 1 2;
    }
    InputModal Static { background: $kit-modal-bg; }
    InputModal Input { background: transparent; border: round $kit-border; }
    InputModal Input:focus { border: round $kit-border-focus; }
    """

    def __init__(self, title: str, placeholder: str = "") -> None:
        super().__init__()
        self._title = title
        self._placeholder = placeholder

    def compose(self) -> ComposeResult:
        with Vertical(id="input-box"):
            yield Static(Text(self._title, style=f"bold {palette.sub}"))
            yield Input(placeholder=self._placeholder, id="input-value")

    def on_mount(self) -> None:
        pop_in(self.query_one("#input-box"))
        settle_pop_in(self, "#input-box")
        self.query_one("#input-value", Input).focus()

    @on(Input.Submitted)
    def _submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        self.dismiss(value or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class LyricsModal(ModalScreen):
    """Lyrics for the current track — scrolling & karaoke-highlighted when the
    server has timed (synced) lines, plain scrollback otherwise.

    Synced lines are `(start_seconds, text)` tuples; the app heartbeat calls
    `tick(position)` at 8fps to advance the highlighted line and keep it
    centered. No timer of its own — one heartbeat drives everything.
    """

    BINDINGS = [
        Binding("escape", "close_modal", show=False),
        Binding("q", "close_modal", show=False),
        Binding("L", "close_modal", show=False),
        # route the main transport keys through the overlay so you can drive
        # playback while reading lyrics
        Binding("space", "app_play_pause", show=False),
        Binding("n", "app_next", show=False),
        Binding("b", "app_prev", show=False),
        Binding("left", "app_seek(-5)", show=False),
        Binding("right", "app_seek(5)", show=False),
        Binding("shift+left", "app_seek(-30)", show=False),
        Binding("shift+right", "app_seek(30)", show=False),
    ]

    DEFAULT_CSS = """
    LyricsModal { align: center middle; background: $kit-overlay; }
    LyricsModal #lyrics-box {
        width: 64; height: auto; max-height: 85%;
        background: $kit-modal-bg; border: round $kit-border-focus; padding: 1 2;
    }
    LyricsModal Static { background: $kit-modal-bg; }
    LyricsModal #lyrics-head { height: 1; }
    LyricsModal #lyrics-body { height: auto; max-height: 32; scrollbar-size-vertical: 1; }
    LyricsModal #lyrics-text { height: auto; }
    """

    def __init__(
        self, title: str, lyrics: str = "", synced: list[tuple[float, str]] | None = None
    ) -> None:
        super().__init__()
        self._title = title
        self._lyrics = lyrics
        self._synced = synced
        self._current = -1  # index of the active synced line

    def compose(self) -> ComposeResult:
        from ricekit.widgets import KitScroll

        with Vertical(id="lyrics-box"):
            with Horizontal(id="lyrics-head"):
                yield Static(Text(self._title, style=f"bold {palette.sub}"), id="lyrics-title")
                if self._synced:
                    # subtle indicator that lines are timed (clock glyph)
                    badge = Text(f"  {icons.CLOCK} ", style=palette.blue)
                    badge.append("synced", style=palette.dim)
                    yield Static(badge, id="lyrics-badge")
            with KitScroll(id="lyrics-body"):
                yield Static(self._render_lyrics(), id="lyrics-text")

    def _render_lyrics(self) -> Text:
        if not self._synced:
            return Text(f"\n{self._lyrics}\n", style=palette.text)
        out = Text("\n")
        for i, (_, line) in enumerate(self._synced):
            if i == self._current:
                style = f"bold {palette.blue}"
            elif abs(i - self._current) == 1:
                style = palette.sub
            else:
                style = palette.dim
            out.append((line or " ") + "\n", style=style)
        out.append("\n")
        return out

    def on_mount(self) -> None:
        pop_in(self.query_one("#lyrics-box"))
        settle_pop_in(self, "#lyrics-box")
        self.query_one("#lyrics-body").focus()

    def tick(self, position: float) -> None:
        """Advance the highlighted synced line from playback position (called
        by the app heartbeat). No-op for plain lyrics; never raises."""
        if not self._synced:
            return
        idx = -1
        for i, (start, _) in enumerate(self._synced):
            if start <= position:
                idx = i
            else:
                break
        if idx == self._current:
            return
        self._current = idx
        try:
            self.query_one("#lyrics-text", Static).update(self._render_lyrics())
            if idx >= 0:
                body = self.query_one("#lyrics-body")
                # +1 for the leading blank line; center the active line
                target = max(0, (idx + 1) - body.size.height // 2)
                body.scroll_to(y=target, animate=False)
        except Exception:
            return  # mid-teardown race

    def action_close_modal(self) -> None:
        self.dismiss(None)

    # forward the transport keys to the app so playback works while lyrics are
    # open; each is a thin, defensive shim (the app owns the real logic)
    def action_app_play_pause(self) -> None:
        try:
            self.app.action_play_pause()
        except Exception:
            pass

    def action_app_next(self) -> None:
        try:
            self.app.action_next_track()
        except Exception:
            pass

    def action_app_prev(self) -> None:
        try:
            self.app.action_prev_track()
        except Exception:
            pass

    def action_app_seek(self, seconds: int) -> None:
        try:
            self.app.action_seek(seconds)
        except Exception:
            pass


class StatsModal(ModalScreen):
    """Local listening stats — a mini "wrapped", read from the play log.

    Purely offline: `summarize` folds the JSONL log into a Summary and this
    renders it with the ricekit palette at paint time (never baked in), so it
    restyles with the theme. Nerd-font icons are `\\uXXXX` escapes. Handles the
    no-history case with an encouraging empty state rather than a blank box.
    """

    BINDINGS = [
        Binding("escape", "close_modal", show=False),
        Binding("q", "close_modal", show=False),
    ]

    DEFAULT_CSS = """
    StatsModal { align: center middle; background: $kit-overlay; }
    StatsModal #stats-box {
        width: 66; height: auto; max-height: 90%;
        background: $kit-modal-bg; border: round $kit-border-focus; padding: 1 2;
    }
    StatsModal Static { background: $kit-modal-bg; }
    StatsModal #stats-head { height: 1; margin-bottom: 1; }
    StatsModal #stats-body { height: auto; max-height: 30; scrollbar-size-vertical: 1; }
    """

    def __init__(self, summary: statsmod.Summary) -> None:
        super().__init__()
        self._summary = summary

    def compose(self) -> ComposeResult:
        from ricekit.widgets import KitScroll

        with Vertical(id="stats-box"):
            with Horizontal(id="stats-head"):
                head = Text()
                head.append(f"{statsmod.ICON_CHART} ", style=palette.mauve)
                head.append("your listening", style=f"bold {palette.sub}")
                yield Static(head, id="stats-title")
            with KitScroll(id="stats-body"):
                yield Static(self._render_stats(), id="stats-text")

    def _render_stats(self) -> Text:
        # (not `_render` — that name is a real internal method on every Widget)
        s = self._summary
        if s.empty:
            out = Text("\n")
            out.append(f"  {statsmod.ICON_MUSIC} no plays logged yet\n\n", style=palette.sub)
            out.append(
                "  play something for a while and it lands here —\n"
                "  counted the same moment it scrobbles.\n",
                style=palette.dim,
            )
            return out

        out = Text("\n")
        # totals line
        out.append("  ", style=palette.text)
        out.append(f"{s.total}", style=f"bold {palette.blue}")
        out.append(" plays all-time", style=palette.dim)
        out.append("   ·   ", style=palette.vfaint)
        out.append(f"{s.week_total}", style=f"bold {palette.green}")
        out.append(" this week", style=palette.dim)
        if s.streak > 0:
            out.append("   ·   ", style=palette.vfaint)
            out.append(f"{statsmod.ICON_FIRE} {s.streak}", style=palette.peach)
            out.append(f" day{'s' if s.streak != 1 else ''}", style=palette.dim)
        out.append("\n\n")

        # activity sparkline over the window
        out.append(f"  {icons.CALENDAR} ", style=palette.lav)
        out.append(f"last {s.days_window} days  ", style=palette.dim)
        out.append(statsmod.sparkline(s.per_day), style=palette.mauve)
        out.append("\n\n")

        self._section(out, f"{icons.STAR} top tracks · this week", s.top_tracks_week
                      or s.top_tracks_all, tracks=True)
        out.append("\n")
        self._section(out, f"{icons.USER} top artists · all time",
                      s.top_artists_all, tracks=False)
        return out

    def _section(self, out: Text, title: str, rows, tracks: bool) -> None:
        out.append(f"  {title}\n", style=f"bold {palette.sub}")
        if not rows:
            out.append("    nothing yet\n", style=palette.dim)
            return
        peak = max((r[-1] for r in rows), default=1) or 1
        for i, row in enumerate(rows):
            count = row[-1]
            marker = f"{i + 1}."
            out.append(f"    {marker:<3}", style=palette.vfaint)
            if tracks:
                title_text, artist = row[0], row[1]
                out.append(title_text, style=palette.text)
                if artist:
                    out.append(f"  {artist}", style=palette.dim)
            else:
                out.append(row[0], style=palette.text)
            # a little count bar so the leader board reads at a glance
            bar = icons.bars(round((count / peak) * 3), palette.blue, palette.vfaint)
            out.append("  ")
            out.append_text(bar)
            out.append(f" {count}\n", style=palette.vfaint)

    def on_mount(self) -> None:
        pop_in(self.query_one("#stats-box"))
        settle_pop_in(self, "#stats-box")
        self.query_one("#stats-body").focus()

    def action_close_modal(self) -> None:
        self.dismiss(None)


class SearchModal(ModalScreen):
    """Global search over artists, albums and songs — debounced, grouped.

    Dismisses with ("song", songs, index) | ("song-queue", song, play_next)
    | ("album", album) | ("artist", artist) | None.
    """

    BINDINGS = [
        Binding("escape", "cancel", show=False),
        Binding("down", "to_list", show=False),
        Binding("a", "queue_song(False)", show=False),
        Binding("A", "queue_song(True)", show=False),
    ]

    DEFAULT_CSS = """
    SearchModal { align: center middle; background: $kit-overlay; }
    SearchModal #search-box {
        width: 72; height: auto; max-height: 80%;
        background: $kit-modal-bg; border: round $kit-border-focus; padding: 1 1;
    }
    SearchModal Input { background: transparent; border: round $kit-border; }
    SearchModal Input:focus { border: round $kit-border-focus; }
    SearchModal #search-results {
        height: auto; max-height: 24;
        text-wrap: nowrap; text-overflow: ellipsis;
    }
    SearchModal #search-hint { padding: 1 1 0 1; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._results = SearchResults()

    def compose(self) -> ComposeResult:
        with Vertical(id="search-box"):
            yield Input(placeholder="search the library", id="search-input")
            yield NavList(id="search-results")
            yield Static(self._hint(), id="search-hint")

    def _hint(self) -> Text:
        t = Text()
        for key, desc in (("enter", "play"), ("a", "queue"), ("A", "play next"), ("esc", "close")):
            if key != "enter":
                t.append("  ·  ", style=palette.vfaint)
            t.append(key, style=palette.blue)
            t.append(f" {desc}", style=palette.dim)
        return t

    def _highlighted_song(self):
        """The Song under the results cursor, or None."""
        ol = self.query_one("#search-results", NavList)
        if ol.highlighted is None:
            return None
        option = ol.get_option_at_index(ol.highlighted)
        if option.id and option.id.startswith("song:"):
            return self._results.songs[int(option.id.split(":", 1)[1])]
        return None

    def action_queue_song(self, play_next: bool) -> None:
        song = self._highlighted_song()
        if song is not None:
            self.dismiss(("song-queue", song, play_next))

    def on_mount(self) -> None:
        pop_in(self.query_one("#search-box"))
        settle_pop_in(self, "#search-box")
        self.query_one("#search-input", Input).focus()

    @on(Input.Changed, "#search-input")
    def _changed(self, event: Input.Changed) -> None:
        query = event.value.strip()
        if len(query) >= 2:
            self._search(query)
        else:
            self.query_one("#search-results", NavList).clear_options()

    @work(exclusive=True, group="search")
    async def _search(self, query: str) -> None:
        try:
            self._results = await self.app.client.search(query)
        except Exception:
            return
        self._render_results()

    def _render_results(self) -> None:
        ol = self.query_one("#search-results", NavList)
        ol.clear_options()
        res = self._results
        opts: list[Option] = []

        def header(label: str) -> None:
            opts.append(Option(Text(f" {label}", style=f"bold {palette.dim}"), disabled=True))

        if res.songs:
            header("songs")
            for i, s in enumerate(res.songs):
                row = Text("  ", no_wrap=True, overflow="ellipsis")
                row.append(anim.NOTE_FRAMES[0] + " ", style=palette.blue)
                row.append(s.title, style=palette.text)
                row.append(f"  {s.artist}", style=palette.dim)
                opts.append(Option(row, id=f"song:{i}"))
        if res.albums:
            header("albums")
            for i, a in enumerate(res.albums):
                row = Text("  ", no_wrap=True, overflow="ellipsis")
                row.append("◉ ", style=palette.mauve)
                row.append(a.name, style=palette.text)
                row.append(f"  {a.artist}", style=palette.dim)
                if a.year:
                    row.append(f" · {a.year}", style=palette.faint)
                opts.append(Option(row, id=f"album:{i}"))
        if res.artists:
            header("artists")
            for i, a in enumerate(res.artists):
                row = Text("  ", no_wrap=True, overflow="ellipsis")
                row.append(f"{icons.USER} ", style=palette.peach)
                row.append(a.name, style=palette.text)
                row.append(f"  {a.album_count} albums", style=palette.dim)
                opts.append(Option(row, id=f"artist:{i}"))
        if not opts:
            opts.append(Option(Text("  no matches", style=palette.dim), disabled=True))
        ol.add_options(opts)
        first = next((i for i, o in enumerate(opts) if not o.disabled), None)
        if first is not None:
            ol.highlighted = first

    def action_to_list(self) -> None:
        ol = self.query_one("#search-results", NavList)
        if ol.option_count:
            ol.focus()

    @on(OptionList.OptionSelected, "#search-results")
    def _selected(self, event: OptionList.OptionSelected) -> None:
        oid = event.option.id
        if not oid:
            return
        kind, _, idx = oid.partition(":")
        i = int(idx)
        if kind == "song":
            self.dismiss(("song", self._results.songs, i))
        elif kind == "album":
            self.dismiss(("album", self._results.albums[i]))
        elif kind == "artist":
            self.dismiss(("artist", self._results.artists[i]))

    def action_cancel(self) -> None:
        self.dismiss(None)


# 10-band EQ presets (gains in dB, low → high), matching player.Player.EQ_FREQS.
EQ_PRESETS: dict[str, list[float]] = {
    "flat":       [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    "bass":       [6, 5, 4, 2, 0, 0, 0, 0, 0, 0],
    "rock":       [4, 3, 1, -1, -1, 1, 2, 3, 3, 3],
    "pop":        [-1, 0, 1, 2, 3, 3, 2, 1, 0, -1],
    "vocal":      [-2, -1, 0, 2, 3, 3, 2, 1, 0, -1],
    "electronic": [4, 3, 1, 0, -2, 1, 0, 1, 3, 4],
    "classical":  [3, 2, 1, 0, 0, 0, -1, -1, 0, 2],
}
EQ_PRESET_ORDER = ["flat", "bass", "rock", "pop", "vocal", "electronic", "classical"]
EQ_FREQ_LABELS = ["31", "62", "125", "250", "500", "1k", "2k", "4k", "8k", "16k"]
EQ_MAX_GAIN = 12.0


class EqualizerModal(ModalScreen):
    """Interactive 10-band equalizer overlay.

    h/l select a band, j/k lower/raise it (±1 dB, ±12 range), space toggles the
    EQ on/off, p cycles presets, r resets to flat. Live-applies to the player
    while enabled. Dismisses with {"enabled", "preset", "bands"}.
    """

    BINDINGS = [
        Binding("escape", "save_close", show=False),
        Binding("q", "save_close", show=False),
        Binding("left", "select(-1)", show=False),
        Binding("h", "select(-1)", show=False),
        Binding("right", "select(1)", show=False),
        Binding("l", "select(1)", show=False),
        Binding("up", "adjust(1)", show=False),
        Binding("k", "adjust(1)", show=False),
        Binding("down", "adjust(-1)", show=False),
        Binding("j", "adjust(-1)", show=False),
        Binding("space", "toggle_enabled", show=False),
        Binding("p", "cycle_preset", show=False),
        Binding("r", "reset", show=False),
    ]

    DEFAULT_CSS = """
    EqualizerModal { align: center middle; background: $kit-overlay; }
    EqualizerModal #eq-box {
        width: 60; height: auto;
        background: $kit-modal-bg; border: round $kit-border-focus; padding: 1 2;
    }
    EqualizerModal Static { background: $kit-modal-bg; }
    EqualizerModal #eq-head { height: 1; margin-bottom: 1; }
    """

    def __init__(self, enabled: bool, preset: str, gains: list[float]) -> None:
        super().__init__()
        self._enabled = bool(enabled)
        self._preset = preset if preset in EQ_PRESETS else "custom"
        self._gains = [float(g) for g in (list(gains)[:10] + [0.0] * 10)[:10]]
        self._sel = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="eq-box"):
            with Horizontal(id="eq-head"):
                head = Text()
                head.append(f"{icons.bars(3, palette.blue, palette.vfaint)} ")
                head.append("equalizer", style=f"bold {palette.sub}")
                yield Static(head, id="eq-title")
            yield Static(self._render(), id="eq-body")
            yield Static(self._hint(), id="eq-hint")

    def _slider(self, gain: float, selected: bool) -> Text:
        # a vertical-ish gauge drawn horizontally: 0 dB centered, ±12 to the ends
        width = 21
        mid = width // 2
        pos = int(round((gain / EQ_MAX_GAIN) * mid)) + mid
        pos = max(0, min(width - 1, pos))
        bar = Text()
        for i in range(width):
            if i == pos:
                bar.append("●", style=palette.blue if selected else palette.sub)
            elif i == mid:
                bar.append("┆", style=palette.vfaint)
            else:
                bar.append("─", style=palette.vfaint)
        return bar

    def _render(self) -> Text:
        out = Text()
        state = "on" if self._enabled else "off"
        state_style = palette.green if self._enabled else palette.faint
        out.append("  state ", style=palette.dim)
        out.append(f"{state}", style=f"bold {state_style}")
        out.append("   preset ", style=palette.dim)
        out.append(f"{self._preset}\n\n", style=palette.mauve)
        for i, gain in enumerate(self._gains):
            selected = i == self._sel
            marker = "▶" if selected else " "
            label_style = f"bold {palette.text}" if selected else palette.dim
            out.append(f" {marker} ", style=palette.blue if selected else palette.vfaint)
            out.append(f"{EQ_FREQ_LABELS[i]:>4} ", style=label_style)
            out.append_text(self._slider(gain, selected))
            out.append(f" {gain:+.0f}\n", style=label_style)
        return out

    def _hint(self) -> Text:
        t = Text("\n")
        pairs = [
            ("h/l", "band"), ("j/k", "gain"), ("space", "on/off"),
            ("p", "preset"), ("r", "reset"), ("esc", "save"),
        ]
        for i, (key, desc) in enumerate(pairs):
            if i:
                t.append("  ·  ", style=palette.vfaint)
            t.append(key, style=palette.blue)
            t.append(f" {desc}", style=palette.dim)
        return t

    def on_mount(self) -> None:
        pop_in(self.query_one("#eq-box"))
        settle_pop_in(self, "#eq-box")

    def _refresh(self) -> None:
        try:
            self.query_one("#eq-body", Static).update(self._render())
        except Exception:
            pass

    def _apply_live(self) -> None:
        # reflect edits in real time only when the EQ is enabled
        player = getattr(self.app, "player", None)
        if player is not None and self._enabled:
            try:
                player.set_equalizer(self._gains)
            except Exception:
                pass

    def action_select(self, delta: int) -> None:
        self._sel = (self._sel + delta) % 10
        self._refresh()

    def action_adjust(self, delta: int) -> None:
        g = max(-EQ_MAX_GAIN, min(EQ_MAX_GAIN, self._gains[self._sel] + delta))
        self._gains[self._sel] = g
        self._preset = "custom"
        self._refresh()
        self._apply_live()

    def action_toggle_enabled(self) -> None:
        self._enabled = not self._enabled
        self._refresh()
        player = getattr(self.app, "player", None)
        if player is not None:
            try:
                player.set_equalizer(self._gains if self._enabled else [])
            except Exception:
                pass

    def action_cycle_preset(self) -> None:
        # step to the next named preset and load its gains
        if self._preset in EQ_PRESET_ORDER:
            idx = (EQ_PRESET_ORDER.index(self._preset) + 1) % len(EQ_PRESET_ORDER)
        else:
            idx = 0
        self._preset = EQ_PRESET_ORDER[idx]
        self._gains = list(EQ_PRESETS[self._preset])
        self._refresh()
        self._apply_live()

    def action_reset(self) -> None:
        self._preset = "flat"
        self._gains = list(EQ_PRESETS["flat"])
        self._refresh()
        self._apply_live()

    def action_save_close(self) -> None:
        self.dismiss({"enabled": self._enabled, "preset": self._preset, "bands": self._gains})


class AudioDeviceSwitcherModal(ModalScreen):
    """Pick the audio output device. Dismisses with the mpv device name, or
    None on escape. `devices` are mpv device dicts; `active` is the current
    device name (marked with a dot)."""

    BINDINGS = [Binding("escape", "cancel", show=False), Binding("q", "cancel", show=False)]

    DEFAULT_CSS = """
    AudioDeviceSwitcherModal { align: center middle; background: $kit-overlay; }
    AudioDeviceSwitcherModal #dev-box {
        width: 60; height: auto; max-height: 80%;
        background: $kit-modal-bg; border: round $kit-border-focus; padding: 1 2;
    }
    AudioDeviceSwitcherModal Static { background: $kit-modal-bg; }
    AudioDeviceSwitcherModal #dev-list { height: auto; max-height: 20; }
    """

    def __init__(self, devices: list[dict], active: str) -> None:
        super().__init__()
        self._devices = devices
        self._active = active

    def compose(self) -> ComposeResult:
        with Vertical(id="dev-box"):
            head = Text()
            head.append(f"{icons.SPEAKER if hasattr(icons, 'SPEAKER') else '♪'} ", style=palette.blue)
            head.append("audio output", style=f"bold {palette.sub}")
            yield Static(head)
            yield NavList(id="dev-list")

    def on_mount(self) -> None:
        pop_in(self.query_one("#dev-box"))
        settle_pop_in(self, "#dev-box")
        ol = self.query_one("#dev-list", NavList)
        opts: list[Option] = []
        if not self._devices:
            opts.append(Option(Text("  no devices reported", style=palette.dim), disabled=True))
        for i, dev in enumerate(self._devices):
            name = dev.get("name", "")
            desc = dev.get("description", "") or name
            row = Text("  ", no_wrap=True, overflow="ellipsis")
            active = name == self._active
            row.append("● " if active else "  ", style=palette.green if active else palette.vfaint)
            row.append(desc, style=palette.text if active else palette.sub)
            opts.append(Option(row, id=f"dev:{i}"))
        ol.add_options(opts)
        first = next((i for i, o in enumerate(opts) if not o.disabled), None)
        if first is not None:
            ol.highlighted = first
        ol.focus()

    @on(OptionList.OptionSelected, "#dev-list")
    def _selected(self, event: OptionList.OptionSelected) -> None:
        oid = event.option.id
        if oid and oid.startswith("dev:"):
            self.dismiss(self._devices[int(oid.split(":", 1)[1])].get("name", ""))

    def action_cancel(self) -> None:
        self.dismiss(None)


class ServerSwitcherModal(ModalScreen):
    """Pick a saved Navidrome profile. Dismisses with the profile name, or None
    on escape. `names` is the list of profile names; `active` is the current."""

    BINDINGS = [Binding("escape", "cancel", show=False), Binding("q", "cancel", show=False)]

    DEFAULT_CSS = """
    ServerSwitcherModal { align: center middle; background: $kit-overlay; }
    ServerSwitcherModal #srv-box {
        width: 54; height: auto; max-height: 80%;
        background: $kit-modal-bg; border: round $kit-border-focus; padding: 1 2;
    }
    ServerSwitcherModal Static { background: $kit-modal-bg; }
    ServerSwitcherModal #srv-list { height: auto; max-height: 18; }
    """

    def __init__(self, names: list[str], active: str) -> None:
        super().__init__()
        self._names = names
        self._active = active

    def compose(self) -> ComposeResult:
        with Vertical(id="srv-box"):
            head = Text()
            head.append(f"{icons.USER} ", style=palette.peach)
            head.append("switch server", style=f"bold {palette.sub}")
            yield Static(head)
            yield NavList(id="srv-list")
            yield Static(self._hint(), id="srv-hint")

    def _hint(self) -> Text:
        t = Text("\n")
        t.append("profiles live in ", style=palette.vfaint)
        t.append("[profiles.<name>]", style=palette.dim)
        t.append(" in your config", style=palette.vfaint)
        return t

    def on_mount(self) -> None:
        pop_in(self.query_one("#srv-box"))
        settle_pop_in(self, "#srv-box")
        ol = self.query_one("#srv-list", NavList)
        opts: list[Option] = []
        if not self._names:
            opts.append(Option(Text("  no profiles configured", style=palette.dim), disabled=True))
        for i, name in enumerate(self._names):
            row = Text("  ", no_wrap=True, overflow="ellipsis")
            active = name == self._active
            row.append("● " if active else "  ", style=palette.green if active else palette.vfaint)
            row.append(name, style=palette.text if active else palette.sub)
            opts.append(Option(row, id=f"srv:{i}"))
        ol.add_options(opts)
        first = next((i for i, o in enumerate(opts) if not o.disabled), None)
        if first is not None:
            ol.highlighted = first
        ol.focus()

    @on(OptionList.OptionSelected, "#srv-list")
    def _selected(self, event: OptionList.OptionSelected) -> None:
        oid = event.option.id
        if oid and oid.startswith("srv:"):
            self.dismiss(self._names[int(oid.split(":", 1)[1])])

    def action_cancel(self) -> None:
        self.dismiss(None)


class PlaylistPickerModal(ModalScreen):
    """Add one track to one or more playlists. space toggles the highlighted
    playlist, ctrl+s confirms. Dismisses with {"action": "add", "playlist_ids":
    [...]} or {"action": "new"} (create a new playlist), or None on escape.

    `playlists` is a list of (id, name).
    """

    BINDINGS = [
        Binding("escape", "cancel", show=False),
        Binding("q", "cancel", show=False),
        Binding("space", "toggle", show=False),
        Binding("ctrl+s", "confirm", show=False),
    ]

    DEFAULT_CSS = """
    PlaylistPickerModal { align: center middle; background: $kit-overlay; }
    PlaylistPickerModal #plp-box {
        width: 56; height: auto; max-height: 80%;
        background: $kit-modal-bg; border: round $kit-border-focus; padding: 1 2;
    }
    PlaylistPickerModal Static { background: $kit-modal-bg; }
    PlaylistPickerModal #plp-list { height: auto; max-height: 20; }
    """

    def __init__(self, playlists: list[tuple[str, str]], title: str = "add to playlists") -> None:
        super().__init__()
        self._playlists = playlists
        self._title = title
        self._checked: set[str] = set()

    def compose(self) -> ComposeResult:
        with Vertical(id="plp-box"):
            yield Static(Text(self._title, style=f"bold {palette.sub}"))
            yield NavList(id="plp-list")
            yield Static(self._hint(), id="plp-hint")

    def _hint(self) -> Text:
        t = Text("\n")
        for i, (key, desc) in enumerate(
            [("space", "toggle"), ("ctrl+s", "add to selected"), ("enter", "new / add"), ("esc", "cancel")]
        ):
            if i:
                t.append("  ·  ", style=palette.vfaint)
            t.append(key, style=palette.blue)
            t.append(f" {desc}", style=palette.dim)
        return t

    def on_mount(self) -> None:
        pop_in(self.query_one("#plp-box"))
        settle_pop_in(self, "#plp-box")
        self._rebuild()
        ol = self.query_one("#plp-list", NavList)
        ol.focus()

    def _rebuild(self) -> None:
        ol = self.query_one("#plp-list", NavList)
        keep = ol.highlighted
        ol.clear_options()
        opts: list[Option] = [Option(Text("  ＋ new playlist…", style=palette.blue), id="new")]
        for pid, name in self._playlists:
            box = "[x]" if pid in self._checked else "[ ]"
            row = Text("  ", no_wrap=True, overflow="ellipsis")
            row.append(f"{box} ", style=palette.green if pid in self._checked else palette.vfaint)
            row.append(name, style=palette.text)
            opts.append(Option(row, id=f"pl:{pid}"))
        ol.add_options(opts)
        if keep is not None:
            ol.highlighted = min(keep, ol.option_count - 1)
        elif ol.option_count:
            ol.highlighted = 0

    def _highlighted_id(self) -> str | None:
        ol = self.query_one("#plp-list", NavList)
        if ol.highlighted is None:
            return None
        return ol.get_option_at_index(ol.highlighted).id

    def action_toggle(self) -> None:
        oid = self._highlighted_id()
        if oid and oid.startswith("pl:"):
            pid = oid.split(":", 1)[1]
            self._checked.symmetric_difference_update({pid})
            self._rebuild()

    def action_confirm(self) -> None:
        if self._checked:
            self.dismiss({"action": "add", "playlist_ids": list(self._checked)})

    @on(OptionList.OptionSelected, "#plp-list")
    def _selected(self, event: OptionList.OptionSelected) -> None:
        oid = event.option.id
        if oid == "new":
            self.dismiss({"action": "new"})
        elif oid and oid.startswith("pl:"):
            # enter on a row: if nothing is checked, add just this one
            if self._checked:
                self.action_confirm()
            else:
                self.dismiss({"action": "add", "playlist_ids": [oid.split(":", 1)[1]]})

    def action_cancel(self) -> None:
        self.dismiss(None)


class SettingsModal(ModalScreen):
    """Album Spotlight / AI settings: pick a provider (Claude or Gemini), paste
    an API key, and optionally set a model. Dismisses with the changed values
    (a dict the app persists to state), or None on escape.

    `current` supplies the effective values to pre-fill.
    """

    BINDINGS = [
        Binding("escape", "cancel", show=False),
        Binding("ctrl+s", "save", show=False),
        Binding("ctrl+g", "cycle_provider", show=False),
        Binding("ctrl+h", "toggle_home", show=False),
    ]

    DEFAULT_CSS = """
    SettingsModal { align: center middle; background: $kit-overlay; }
    SettingsModal #set-box {
        width: 64; height: auto;
        background: $kit-modal-bg; border: round $kit-border-focus; padding: 1 2;
    }
    SettingsModal Static { background: $kit-modal-bg; }
    SettingsModal Input { background: transparent; border: round $kit-border; margin-bottom: 0; }
    SettingsModal Input:focus { border: round $kit-border-focus; }
    SettingsModal .set-label { height: 1; }
    """

    def __init__(self, current: dict) -> None:
        super().__init__()
        from navitui import ai as aimod

        self._ai = aimod
        self._provider = current.get("ai_provider", "anthropic")
        if self._provider not in aimod.PROVIDERS:
            self._provider = "anthropic"
        self._home = bool(current.get("home_spotlight", True))
        self._anthropic_key = current.get("anthropic_api_key", "")
        self._gemini_key = current.get("gemini_api_key", "")
        self._model = current.get("ai_model", "")

    def compose(self) -> ComposeResult:
        with Vertical(id="set-box"):
            head = Text()
            head.append("⚙ ", style=palette.mauve)
            head.append("Album Spotlight — AI settings", style=f"bold {palette.sub}")
            yield Static(head)
            yield Static(self._status(), id="set-status")
            yield Static(Text("Anthropic (Claude) API key", style=palette.dim), classes="set-label")
            yield Input(value=self._anthropic_key, password=True,
                        placeholder="sk-ant-…", id="set-anthropic")
            yield Static(Text("Gemini (Google) API key", style=palette.dim), classes="set-label")
            yield Input(value=self._gemini_key, password=True,
                        placeholder="AIza…", id="set-gemini")
            yield Static(Text("Model (blank = provider default)", style=palette.dim), classes="set-label")
            yield Input(value=self._model, placeholder=self._ai.default_model(self._provider),
                        id="set-model")
            yield Static(self._hint(), id="set-hint")

    def _status(self) -> Text:
        t = Text()
        t.append("Provider ", style=palette.dim)
        t.append(self._ai.provider_label(self._provider), style=f"bold {palette.blue}")
        t.append("   Home ", style=palette.dim)
        t.append("on" if self._home else "off",
                 style=f"bold {palette.green if self._home else palette.faint}")
        t.append("   default model ", style=palette.dim)
        t.append(self._ai.default_model(self._provider), style=palette.mauve)
        return t

    def _hint(self) -> Text:
        t = Text("\n")
        for i, (key, desc) in enumerate(
            [("ctrl+g", "switch provider"), ("ctrl+h", "toggle Home"),
             ("ctrl+s", "save"), ("esc", "cancel")]
        ):
            if i:
                t.append("  ·  ", style=palette.vfaint)
            t.append(key, style=palette.blue)
            t.append(f" {desc}", style=palette.dim)
        return t

    def on_mount(self) -> None:
        pop_in(self.query_one("#set-box"))
        settle_pop_in(self, "#set-box")
        self.query_one("#set-anthropic", Input).focus()

    def _refresh_status(self) -> None:
        try:
            self.query_one("#set-status", Static).update(self._status())
            self.query_one("#set-model", Input).placeholder = self._ai.default_model(self._provider)
        except Exception:
            pass

    def action_cycle_provider(self) -> None:
        idx = (self._ai.PROVIDERS.index(self._provider) + 1) % len(self._ai.PROVIDERS)
        self._provider = self._ai.PROVIDERS[idx]
        self._refresh_status()

    def action_toggle_home(self) -> None:
        self._home = not self._home
        self._refresh_status()

    @on(Input.Submitted)
    def _submitted(self, event: Input.Submitted) -> None:
        self.action_save()

    def action_save(self) -> None:
        self.dismiss(
            {
                "ai_provider": self._provider,
                "anthropic_api_key": self.query_one("#set-anthropic", Input).value.strip(),
                "gemini_api_key": self.query_one("#set-gemini", Input).value.strip(),
                "ai_model": self.query_one("#set-model", Input).value.strip(),
                "home_spotlight": self._home,
            }
        )

    def action_cancel(self) -> None:
        self.dismiss(None)
