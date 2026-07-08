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

from navitui import anim
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
