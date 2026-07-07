"""NaviTui — the app.

Cache-first everywhere: every pane renders from the last-known JSON cache
instantly, then a worker fetches fresh rows and swaps them in silently.
One 8fps heartbeat drives every animation (logo shimmer, visualizer,
progress pulse, marquee, spinners); each tick repaints only a few cells.
"""

from __future__ import annotations

import asyncio
import random

from rich.text import Text
from textual import on, work
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, OptionList, Static
from textual.widgets.option_list import Option

from ricekit import KitApp, icons, palette
from ricekit.modals import HelpModal
from ricekit.storage import AppDirs
from ricekit.widgets import NavList, Splitter, pop_in

from navitui import anim, player as playermod
from navitui.api import SubsonicClient, SubsonicError
from navitui.art import CoverArt
from navitui.models import Album, Artist, Playlist, Song
from navitui.playqueue import PlayQueue, Repeat
from navitui.screens import OnboardingScreen, SearchModal
from navitui.widgets import Logo, NowPlaying, PAUSE_GLYPH, PLAY_GLYPH

TABS = ["library", "albums", "playlists", "starred"]

ALBUM_VIEWS = [
    ("newest", "recently added"),
    ("recent", "recently played"),
    ("frequent", "most played"),
    ("random", "random albums"),
    ("alphabeticalByName", "all albums"),
    ("all-songs", "all tracks"),
    ("random-songs", "surprise me"),
    ("shuffle-all", "shuffle everything"),
]

SONG_VIEWS = {"all-songs", "random-songs", "shuffle-all"}

HELP_SECTIONS = [
    (
        "playback",
        [
            ("space", "play / pause"),
            ("enter", "play track / album / playlist"),
            ("n / b", "next / previous track"),
            ("← / →", "seek 5s   (shift: 30s)"),
            ("- / +", "volume down / up"),
            ("m", "mute"),
            ("s", "toggle shuffle"),
            ("r", "cycle repeat  off → all → one"),
        ],
    ),
    (
        "queue",
        [
            ("a", "add highlighted to queue"),
            ("A", "play highlighted next"),
            ("x", "remove track (in queue panel)"),
            ("X", "clear queue"),
        ],
    ),
    (
        "browse",
        [
            ("1-4", "library · albums · playlists · starred"),
            ("h / l", "previous / next panel"),
            ("j / k / g / G", "move in lists"),
            ("/", "search everything"),
            ("f", "star / unstar highlighted"),
            ("R", "refresh from server"),
        ],
    ),
    (
        "app",
        [
            ("t", "cycle kit themes"),
            ("T", "theme picker (live preview)"),
            ("?", "this help"),
            ("q", "quit"),
        ],
    ),
]


class NaviTuiApp(KitApp):
    TITLE = "NaviTui"

    BINDINGS = [
        Binding("space", "play_pause", "play/pause"),
        Binding("n", "next_track", "next"),
        Binding("b", "prev_track", show=False),
        Binding("slash", "search", "search"),
        Binding("s", "toggle_shuffle", "shuffle"),
        Binding("r", "cycle_repeat", "repeat"),
        Binding("left", "seek(-5)", show=False),
        Binding("right", "seek(5)", show=False),
        Binding("shift+left", "seek(-30)", show=False),
        Binding("shift+right", "seek(30)", show=False),
        Binding("minus", "volume(-5)", show=False),
        Binding("plus,equals_sign", "volume(5)", show=False),
        Binding("m", "mute", show=False),
        Binding("a", "enqueue(False)", show=False),
        Binding("A", "enqueue(True)", show=False),
        Binding("x", "queue_remove", show=False),
        Binding("X", "queue_clear", show=False),
        Binding("f", "star", show=False),
        Binding("1", "set_tab('library')", show=False),
        Binding("2", "set_tab('albums')", show=False),
        Binding("3", "set_tab('playlists')", show=False),
        Binding("4", "set_tab('starred')", show=False),
        Binding("h", "focus_panel(-1)", show=False),
        Binding("l", "focus_panel(1)", show=False),
        Binding("R", "refresh", show=False),
        Binding("t", "cycle_kit_theme", "theme"),
        Binding("T", "change_theme", show=False),
        Binding("question_mark", "help", "help"),
        Binding("q", "quit", "quit"),
    ]

    CSS = """
    #topbar { height: 1; padding: 0 1; }
    #topbar #tabs { width: auto; padding: 0 2; link-style: none; link-color: $text-muted; }
    #topbar #status { width: 1fr; text-align: right; }

    #main { height: 1fr; }
    NavList { text-wrap: nowrap; text-overflow: ellipsis; }
    .panel { border: round $kit-border; }
    .panel:focus-within { border: round $kit-border-focus; }
    .panel NavList { height: 1fr; }
    #pane1-panel { width: 26; }
    #pane2-panel { width: 34; }
    #pane3-panel { width: 1fr; }
    #side { width: 36; }
    #art-panel { height: 40%; min-height: 12; border: round $kit-border; }
    #queue-panel { height: 1fr; border: round $kit-border; }

    NowPlaying.playing { border: round $kit-border-alt; }

    .hidden { display: none; }
    """

    def __init__(self, client: SubsonicClient | None = None, ao: str | None = None) -> None:
        super().__init__()
        self.dirs = AppDirs("navitui")
        self.client: SubsonicClient | None = client
        self._ao = ao
        self.queue = PlayQueue()
        self.player = None
        # pane backing data
        self._artists: list[Artist] = []
        self._pane1_playlists: list[Playlist] = []
        self._albums: list[Album] = []
        self._songs: list[Song] = []
        self._starred_songs: list[Song] = []
        self.tab = "library"
        # playback bookkeeping
        self._scrobbled = False
        self._end_failures = 0
        self._resume_position = 0.0
        self._mutations = 0
        self._last_persist = 0.0

    # ── layout ────────────────────────────────────────────────────────
    def compose(self):
        with Horizontal(id="topbar"):
            yield Logo(id="logo")
            yield Static(id="tabs")
            yield Static(id="status")
        with Horizontal(id="main"):
            with Vertical(id="pane1-panel", classes="panel"):
                yield NavList(id="pane1-list")
            yield Splitter("#pane1-panel", on_resized=self._persist_width, id="split1")
            with Vertical(id="pane2-panel", classes="panel"):
                yield NavList(id="pane2-list")
            yield Splitter("#pane2-panel", on_resized=self._persist_width, id="split2")
            with Vertical(id="pane3-panel", classes="panel"):
                yield NavList(id="pane3-list")
            yield Splitter("#side", invert=True, on_resized=self._persist_width, id="split3")
            with Vertical(id="side"):
                yield CoverArt(id="art-panel")
                with Vertical(id="queue-panel", classes="panel"):
                    yield NavList(id="queue-list")
        yield NowPlaying(id="now")
        yield Footer()

    def on_mount(self) -> None:
        self._loop = asyncio.get_running_loop()  # for mpv-thread callbacks
        state = self.dirs.load_state()
        self.init_kit(theme=state.get("theme"))

        for selector, width in (state.get("widths") or {}).items():
            try:
                self.query_one(selector).styles.width = width
            except Exception:
                pass

        self.query_one("#art-panel", CoverArt).border_title = "cover"
        self.query_one("#queue-panel").border_title = "queue"
        self.tab = state.get("tab", "library")
        self._render_tabs()

        self.player = playermod.create_player(self._mpv_position, self._mpv_track_end, ao=self._ao)
        self.player.set_volume(int(state.get("volume", 80)))
        now = self.query_one("#now", NowPlaying)
        now.volume = self.player.volume

        # restore the queue exactly as it was left
        cached_queue = self.dirs.read_cache("queue")
        if cached_queue:
            self.queue = PlayQueue.from_dict(cached_queue)
            self._resume_position = float(cached_queue.get("position", 0.0))
            now.set_song(self.queue.current)
            now.set_progress(self._resume_position, self.queue.current.duration if self.queue.current else 0)
            now._title_flash = 0
        now.shuffle = self.queue.shuffle
        now.repeat = self.queue.repeat
        self._render_queue()

        self.set_interval(1 / 8, self._heartbeat)
        self.set_interval(180, self._maybe_auto_refresh)

        if not playermod.MPV_AVAILABLE:
            self.notify(playermod.INSTALL_HINTS, severity="warning", timeout=15)

        if self.client is None:
            config = self.dirs.load_config()
            if all(config.get(k) for k in ("server", "username", "token", "salt")):
                self.client = SubsonicClient(
                    config["server"], config["username"], config["token"], config["salt"],
                    art_dir=self.dirs.cache_dir / "art",
                )
            else:
                self.push_screen(
                    OnboardingScreen(config.get("server", ""), config.get("username", "")),
                    self._onboarded,
                )
                return
        self._start()

    def _onboarded(self, config: dict | None) -> None:
        if not config:
            return
        self._save_secrets(config)
        self.client = SubsonicClient(
            config["server"], config["username"], config["token"], config["salt"],
            art_dir=self.dirs.cache_dir / "art",
        )
        self.notify("welcome to NaviTui ♪", timeout=4)
        self._start()

    def _save_secrets(self, config: dict) -> None:
        path = self.dirs.config_file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(f'{k} = "{v}"\n' for k, v in config.items()))
        path.chmod(0o600)

    def _start(self) -> None:
        self._render_status()
        self._show_tab(initial=True)
        self.query_one("#pane1-list", NavList).focus()

    def _render_status(self) -> None:
        if self.client is None:
            return
        host = self.client.server.split("://", 1)[-1]
        self.query_one("#status", Static).update(
            Text(f"{self.client.username}@{host}", style=palette.dim)
        )

    # ── the heartbeat (all constant animation) ────────────────────────
    def _heartbeat(self) -> None:
        try:
            self.query_one("#logo", Logo).tick()
            now = self.query_one("#now", NowPlaying)
            if self.player is not None:
                now.set_playing(self.player.active and not self.player.paused)
                now.set_class(self.player.active, "playing")
            now.tick()
            busy = any(
                not w.is_finished
                for w in self.workers
                if w.group in ("lib", "albums", "songs", "albumlist", "starred")
            )
            pane1 = self.query_one("#pane1-panel")
            if busy:
                pane1.border_subtitle = f"{anim.spinner(int(now._tick))} refreshing"
            elif pane1.border_subtitle and "refreshing" in pane1.border_subtitle:
                count = self.query_one("#pane1-list", NavList).option_count
                pane1.border_subtitle = str(count) if count else None
        except Exception:
            return  # shutdown race: the timer can fire while widgets unmount

    # ── tabs ──────────────────────────────────────────────────────────

    def _render_tabs(self) -> None:
        parts = []
        for name in TABS:
            if name == self.tab:
                parts.append(f"[bold {palette.blue}] {name} [/]")
            else:
                parts.append(f"[{palette.dim}][@click=app.set_tab('{name}')] {name} [/][/]")
        self.query_one("#tabs", Static).update("·".join(parts))

    def action_set_tab(self, tab: str) -> None:
        if tab not in TABS or tab == self.tab:
            return
        self.tab = tab
        self.dirs.save_state({"tab": tab})
        self._render_tabs()
        self._show_tab()

    def _show_tab(self, initial: bool = False) -> None:
        pane2 = self.query_one("#pane2-panel")
        split2 = self.query_one("#split2")
        show_pane2 = self.tab != "playlists"
        pane2.set_class(not show_pane2, "hidden")
        split2.set_class(not show_pane2, "hidden")

        p1 = self.query_one("#pane1-panel")
        p2 = self.query_one("#pane2-panel")
        p3 = self.query_one("#pane3-panel")
        titles = {
            "library": ("artists", "albums", "tracks"),
            "albums": ("views", "albums", "tracks"),
            "playlists": ("playlists", "", "tracks"),
            "starred": ("★ artists", "★ albums", "★ tracks"),
        }[self.tab]
        p1.border_title, p2.border_title, p3.border_title = titles
        for panel in (p1, p2, p3):
            panel.border_subtitle = None
        pop_in(p1)
        if show_pane2:
            pop_in(p2)
        pop_in(p3)

        self._fill("#pane2-list", [])
        self._fill("#pane3-list", [])
        if self.tab == "library":
            self._load_artists()
        elif self.tab == "albums":
            self._fill(
                "#pane1-list",
                [self._view_row(vid, label) for vid, label in ALBUM_VIEWS],
            )
        elif self.tab == "playlists":
            self._load_playlists()
        elif self.tab == "starred":
            self._load_starred()
        if not initial:
            self.query_one("#pane1-list", NavList).focus()

    # ── row rendering ─────────────────────────────────────────────────
    @staticmethod
    def _row() -> Text:
        return Text(no_wrap=True, overflow="ellipsis")

    def _artist_row(self, a: Artist) -> Option:
        row = self._row()
        row.append(f"{icons.USER} ", style=palette.peach if a.starred else palette.faint)
        row.append(a.name, style=palette.text)
        row.append(f" {a.album_count}", style=palette.vfaint)
        return Option(row, id=a.id)

    def _album_row(self, a: Album) -> Option:
        row = self._row()
        row.append("◉ ", style=palette.mauve)
        row.append(a.name, style=palette.text)
        if a.starred:
            row.append(f" {icons.STAR}", style=palette.yellow)
        meta = f" {a.year}" if a.year else ""
        row.append(f"{meta} · {a.song_count}♪", style=palette.vfaint)
        return Option(row, id=a.id)

    def _song_row(self, s: Song, number: int | None = None) -> Option:
        current = self.queue.current
        is_current = current is not None and s.id == current.id
        row = self._row()
        marker = anim.NOTE_FRAMES[0] if is_current else (f"{number:>2d}" if number else " ·")
        row.append(f"{marker} ", style=palette.blue if is_current else palette.vfaint)
        row.append(s.title, style=f"bold {palette.blue}" if is_current else palette.text)
        if s.starred:
            row.append(f" {icons.STAR}", style=palette.yellow)
        row.append(f"  {s.artist}", style=palette.dim)
        row.append(f" · {anim.fmt_time(s.duration)}", style=palette.vfaint)
        return Option(row, id=s.id)

    def _playlist_row(self, p: Playlist) -> Option:
        row = self._row()
        row.append(f"{icons.LIST} ", style=palette.lav)
        row.append(p.name, style=palette.text)
        row.append(f"  {p.song_count}♪ · {anim.fmt_time(p.duration)}", style=palette.vfaint)
        return Option(row, id=p.id)

    def _view_row(self, view_id: str, label: str) -> Option:
        row = self._row()
        row.append("◍ ", style=palette.mauve)
        row.append(label, style=palette.text)
        return Option(row, id=view_id)

    def _fill(self, selector: str, options: list[Option], subtitle_of: str | None = None) -> None:
        ol = self.query_one(selector, NavList)
        had_focus = ol.has_focus
        highlighted = ol.highlighted
        ol.clear_options()
        ol.add_options(options)
        if options:
            keep = highlighted if highlighted is not None and highlighted < len(options) else 0
            ol.highlighted = keep
        if subtitle_of:
            panel = self.query_one(subtitle_of)
            panel.border_subtitle = str(len(options)) if options else None
        if had_focus:
            ol.focus()

    # ── library tab ───────────────────────────────────────────────────
    @work(exclusive=True, group="lib")
    async def _load_artists(self) -> None:
        cached = self.dirs.read_cache("artists")
        if cached:
            self._artists = [Artist.from_dict(a) for a in cached.get("artists", [])]
            self._fill("#pane1-list", [self._artist_row(a) for a in self._artists], "#pane1-panel")
        try:
            self._artists = await self.client.get_artists()
        except Exception as e:
            self._connection_trouble(e)
            return
        self.dirs.write_cache("artists", {"artists": [a.to_dict() for a in self._artists]})
        if self.tab == "library":
            self._fill("#pane1-list", [self._artist_row(a) for a in self._artists], "#pane1-panel")

    @work(exclusive=True, group="albums")
    async def _load_artist_albums(self, artist: Artist) -> None:
        await asyncio.sleep(0.12)  # superseded while the cursor is moving
        key = f"artist-albums-{artist.id}"
        cached = self.dirs.read_cache(key)
        if cached:
            self._albums = [Album.from_dict(a) for a in cached.get("albums", [])]
            self._fill("#pane2-list", [self._album_row(a) for a in self._albums], "#pane2-panel")
        try:
            albums = await self.client.get_artist_albums(artist.id)
        except Exception:
            return
        self.dirs.write_cache(key, {"albums": [a.to_dict() for a in albums]})
        if self.tab == "library":
            self._albums = albums
            self._fill("#pane2-list", [self._album_row(a) for a in albums], "#pane2-panel")

    @work(exclusive=True, group="songs")
    async def _load_album_songs(self, album: Album) -> None:
        await asyncio.sleep(0.12)
        if not (self.player and self.player.active) and album.cover_art:
            self._load_art(album.cover_art, f"album-{album.id}")
        key = f"album-songs-{album.id}"
        cached = self.dirs.read_cache(key)
        if cached:
            self._songs = [Song.from_dict(s) for s in cached.get("songs", [])]
            self._fill("#pane3-list", [self._song_row(s, s.track) for s in self._songs], "#pane3-panel")
        try:
            songs = await self.client.get_album_songs(album.id)
        except Exception:
            return
        self.dirs.write_cache(key, {"songs": [s.to_dict() for s in songs]})
        self._songs = songs
        self._fill("#pane3-list", [self._song_row(s, s.track) for s in songs], "#pane3-panel")

    # ── albums tab ────────────────────────────────────────────────────
    @work(exclusive=True, group="albumlist")
    async def _load_album_view(self, view: str) -> None:
        await asyncio.sleep(0.12)
        if view == "random-songs":
            try:
                songs = await self.client.get_random_songs(100)
            except Exception as e:
                self._connection_trouble(e)
                return
            self._songs = songs
            self._fill("#pane2-list", [])
            self._fill("#pane3-list", [self._song_row(s) for s in songs], "#pane3-panel")
            return
        if view in ("all-songs", "shuffle-all"):
            cached = self.dirs.read_cache("all-songs")
            if cached:
                self._songs = [Song.from_dict(s) for s in cached.get("songs", [])]
                self._fill("#pane2-list", [])
                self._fill("#pane3-list", [self._song_row(s) for s in self._songs], "#pane3-panel")
            try:
                songs = await self.client.get_all_songs()
            except Exception as e:
                self._connection_trouble(e)
                return
            self.dirs.write_cache("all-songs", {"songs": [s.to_dict() for s in songs]})
            if self.tab == "albums":
                self._songs = songs
                self._fill("#pane2-list", [])
                self._fill("#pane3-list", [self._song_row(s) for s in songs], "#pane3-panel")
            return
        key = f"albumlist-{view}"
        cached = None if view == "random" else self.dirs.read_cache(key)
        if cached:
            self._albums = [Album.from_dict(a) for a in cached.get("albums", [])]
            self._fill("#pane2-list", [self._album_row(a) for a in self._albums], "#pane2-panel")
        try:
            albums = await self.client.get_album_list(view)
        except Exception as e:
            self._connection_trouble(e)
            return
        if view != "random":
            self.dirs.write_cache(key, {"albums": [a.to_dict() for a in albums]})
        if self.tab == "albums":
            self._albums = albums
            self._fill("#pane2-list", [self._album_row(a) for a in albums], "#pane2-panel")

    # ── playlists tab ─────────────────────────────────────────────────
    @work(exclusive=True, group="lib")
    async def _load_playlists(self) -> None:
        cached = self.dirs.read_cache("playlists")
        if cached:
            self._pane1_playlists = [Playlist.from_dict(p) for p in cached.get("playlists", [])]
            self._fill("#pane1-list", [self._playlist_row(p) for p in self._pane1_playlists], "#pane1-panel")
        try:
            playlists = await self.client.get_playlists()
        except Exception as e:
            self._connection_trouble(e)
            return
        self.dirs.write_cache("playlists", {"playlists": [p.to_dict() for p in playlists]})
        if self.tab == "playlists":
            self._pane1_playlists = playlists
            self._fill("#pane1-list", [self._playlist_row(p) for p in playlists], "#pane1-panel")

    @work(exclusive=True, group="songs")
    async def _load_playlist_songs(self, playlist: Playlist) -> None:
        await asyncio.sleep(0.12)
        key = f"playlist-songs-{playlist.id}"
        cached = self.dirs.read_cache(key)
        if cached:
            self._songs = [Song.from_dict(s) for s in cached.get("songs", [])]
            self._fill("#pane3-list", [self._song_row(s) for s in self._songs], "#pane3-panel")
        try:
            songs = await self.client.get_playlist_songs(playlist.id)
        except Exception:
            return
        self.dirs.write_cache(key, {"songs": [s.to_dict() for s in songs]})
        if self.tab == "playlists":
            self._songs = songs
            self._fill("#pane3-list", [self._song_row(s) for s in songs], "#pane3-panel")

    # ── starred tab ───────────────────────────────────────────────────
    @work(exclusive=True, group="lib")
    async def _load_starred(self) -> None:
        cached = self.dirs.read_cache("starred")
        if cached:
            self._apply_starred(
                [Artist.from_dict(a) for a in cached.get("artists", [])],
                [Album.from_dict(a) for a in cached.get("albums", [])],
                [Song.from_dict(s) for s in cached.get("songs", [])],
            )
        try:
            starred = await self.client.get_starred()
        except Exception as e:
            self._connection_trouble(e)
            return
        self.dirs.write_cache(
            "starred",
            {
                "artists": [a.to_dict() for a in starred.artists],
                "albums": [a.to_dict() for a in starred.albums],
                "songs": [s.to_dict() for s in starred.songs],
            },
        )
        if self.tab == "starred":
            self._apply_starred(starred.artists, starred.albums, starred.songs)

    def _apply_starred(self, artists: list[Artist], albums: list[Album], songs: list[Song]) -> None:
        self._artists = artists
        self._albums = albums
        self._songs = songs
        self._starred_songs = songs
        self._fill("#pane1-list", [self._artist_row(a) for a in artists], "#pane1-panel")
        self._fill("#pane2-list", [self._album_row(a) for a in albums], "#pane2-panel")
        self._fill("#pane3-list", [self._song_row(s) for s in songs], "#pane3-panel")

    # ── selection plumbing ────────────────────────────────────────────
    @on(OptionList.OptionHighlighted, "#pane1-list")
    def _pane1_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        oid = event.option.id
        if not oid:
            return
        if self.tab == "library":
            artist = next((a for a in self._artists if a.id == oid), None)
            if artist:
                self._load_artist_albums(artist)
        elif self.tab == "albums":
            self._load_album_view(oid)
        elif self.tab == "playlists":
            playlist = next((p for p in self._pane1_playlists if p.id == oid), None)
            if playlist:
                self._load_playlist_songs(playlist)

    @on(OptionList.OptionSelected, "#pane1-list")
    def _pane1_selected(self, event: OptionList.OptionSelected) -> None:
        if self.tab == "playlists":
            if self._songs:
                self._play_songs(self._songs, 0)
        elif self.tab == "starred":
            pass
        elif self.tab == "albums" and event.option.id == "shuffle-all":
            self._shuffle_everything()
        elif self.tab == "albums" and event.option.id in SONG_VIEWS:
            self.query_one("#pane3-list", NavList).focus()
        else:
            self.query_one("#pane2-list", NavList).focus()

    def _shuffle_everything(self) -> None:
        if not self._songs:
            self.notify("still fetching the library — try again in a second", timeout=3)
            return
        if not self.queue.shuffle:
            self.queue.shuffle = True
            self.query_one("#now", NowPlaying).shuffle = True
            self.dirs.save_state({"shuffle": True})
        self._play_songs(self._songs, random.randrange(len(self._songs)))
        self.notify(f"shuffling all {len(self._songs)} tracks", timeout=3)

    @on(OptionList.OptionHighlighted, "#pane2-list")
    def _pane2_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if self.tab == "starred":
            return  # starred panes are independent
        oid = event.option.id
        album = next((a for a in self._albums if a.id == oid), None)
        if album:
            self._load_album_songs(album)

    @on(OptionList.OptionSelected, "#pane2-list")
    def _pane2_selected(self, event: OptionList.OptionSelected) -> None:
        album = next((a for a in self._albums if a.id == event.option.id), None)
        if album:
            self._play_album(album)

    @on(OptionList.OptionSelected, "#pane3-list")
    def _pane3_selected(self, event: OptionList.OptionSelected) -> None:
        idx = next((i for i, s in enumerate(self._songs) if s.id == event.option.id), None)
        if idx is not None:
            self._play_songs(self._songs, idx)

    @on(OptionList.OptionSelected, "#queue-list")
    def _queue_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.highlighted is not None:
            self.queue.jump(event.option_list.highlighted)
            self._play_current()

    # ── playback ──────────────────────────────────────────────────────
    def _play_album(self, album: Album) -> None:
        async def load_and_play() -> None:
            key = f"album-songs-{album.id}"
            cached = self.dirs.read_cache(key)
            songs = [Song.from_dict(s) for s in (cached or {}).get("songs", [])]
            if not songs:
                try:
                    songs = await self.client.get_album_songs(album.id)
                    self.dirs.write_cache(key, {"songs": [s.to_dict() for s in songs]})
                except Exception as e:
                    self._connection_trouble(e)
                    return
            self._play_songs(songs, 0)

        self.run_worker(load_and_play(), group="mutate")

    def _play_songs(self, songs: list[Song], start: int) -> None:
        self.queue.set_songs(songs, start)
        self._play_current()

    def _play_current(self, resume_at: float = 0.0) -> None:
        song = self.queue.current
        now = self.query_one("#now", NowPlaying)
        if song is None:
            self.player.stop()
            now.set_song(None)
            self._render_queue()
            return
        self.player.play(self.client.stream_url(song.id), start=resume_at)
        now.set_song(song)
        now.set_progress(resume_at, song.duration)
        self._scrobbled = False
        self._scrobble(song.id, False)
        if song.cover_art:
            self._load_art(song.cover_art, f"song-{song.id}")
        self._render_queue()
        self._refresh_song_markers()
        self._persist_queue()

    def _refresh_song_markers(self) -> None:
        """Re-render the tracks pane so the ♪ marker follows the player."""
        if self.tab in ("library",):
            self._fill("#pane3-list", [self._song_row(s, s.track) for s in self._songs])
        else:
            self._fill("#pane3-list", [self._song_row(s) for s in self._songs])

    def action_play_pause(self) -> None:
        if self.player.active:
            self.player.toggle_pause()
        elif self.queue.current is not None:
            # resume a restored queue exactly where it left off
            self._play_current(resume_at=self._resume_position)
            self._resume_position = 0.0

    def action_next_track(self) -> None:
        song = self.queue.advance(natural=False)
        if song is not None:
            self._play_current()

    def action_prev_track(self) -> None:
        if self.player.position > 4:
            self.player.seek_to(0.0)
            return
        song = self.queue.prev()
        if song is not None:
            self._play_current()

    def action_seek(self, seconds: int) -> None:
        self.player.seek(seconds)

    def seek_fraction(self, fraction: float) -> None:
        self.player.seek_to(fraction)

    def action_volume(self, delta: int) -> None:
        volume = self.player.set_volume(self.player.volume + delta)
        now = self.query_one("#now", NowPlaying)
        now.volume = volume
        now.flash_volume()
        self.dirs.save_state({"volume": volume})

    def set_volume_fraction(self, fraction: float) -> None:
        self.action_volume(round(fraction * 100) - self.player.volume)

    def action_mute(self) -> None:
        now = self.query_one("#now", NowPlaying)
        now.muted = self.player.toggle_mute()
        now.flash_volume()

    def action_toggle_shuffle(self) -> None:
        on_now = self.queue.toggle_shuffle()
        self.query_one("#now", NowPlaying).shuffle = on_now
        self._render_queue()
        self.dirs.save_state({"shuffle": on_now})
        self.notify(f"shuffle {'on' if on_now else 'off'}", timeout=1.5)

    def action_cycle_repeat(self) -> None:
        mode = self.queue.cycle_repeat()
        self.query_one("#now", NowPlaying).repeat = mode
        self.dirs.save_state({"repeat": mode.value})
        self.notify(f"repeat {mode.value}", timeout=1.5)

    # ── mpv thread callbacks ──────────────────────────────────────────
    # These arrive on mpv's event thread and must NEVER block: a blocking
    # call_from_thread here deadlocks against player.terminate() on quit
    # (the UI thread joins the event thread while the event thread waits
    # for the UI thread). call_soon_threadsafe just enqueues and returns.
    def _mpv_position(self, position: float, duration: float) -> None:
        try:
            self._loop.call_soon_threadsafe(self._on_position, position, duration)
        except Exception:
            pass  # loop gone — app shutting down

    def _mpv_track_end(self, failed: bool) -> None:
        try:
            self._loop.call_soon_threadsafe(self._on_track_end, failed)
        except Exception:
            pass

    def _on_position(self, position: float, duration: float) -> None:
        if not self.is_running:
            return
        now = self.query_one("#now", NowPlaying)
        now.set_progress(position, duration)
        if position > 3:
            self._end_failures = 0
        song = self.queue.current
        if song and not self._scrobbled and duration > 0:
            if position >= min(duration / 2, 240):
                self._scrobbled = True
                self._scrobble(song.id, True)
        # crash-safe resume point, at most every 10s
        if position - self._last_persist >= 10 or position < self._last_persist:
            self._last_persist = position
            self._persist_queue(position)

    def _on_track_end(self, failed: bool) -> None:
        if not self.is_running:
            return
        if failed:
            self._end_failures += 1
            song = self.queue.current
            self.notify(
                f"stream failed: {song.title if song else '?'}",
                severity="warning",
                timeout=4,
            )
            if self._end_failures >= 3:
                self.notify("three failures in a row — stopping", severity="error")
                self.player.stop()
                self.query_one("#now", NowPlaying).set_playing(False)
                return
        song = self.queue.advance(natural=not failed)
        if song is not None:
            self._play_current()
        else:
            self.player.stop()
            now = self.query_one("#now", NowPlaying)
            now.set_playing(False)
            now.set_progress(0.0, 0.0)
            self._render_queue()

    # ── queue ─────────────────────────────────────────────────────────
    def _render_queue(self) -> None:
        panel = self.query_one("#queue-panel")
        options = []
        for i, song in enumerate(self.queue.songs):
            row = self._row()
            if i == self.queue.index:
                glyph = PLAY_GLYPH if (self.player and self.player.active and not self.player.paused) else PAUSE_GLYPH
                row.append(f"{glyph} ", style=palette.green)
                row.append(song.title, style=f"bold {palette.blue}")
            else:
                row.append(f"{i + 1:>2d} ", style=palette.vfaint)
                row.append(song.title, style=palette.text)
            row.append(f"  {song.artist}", style=palette.dim)
            options.append(Option(row, id=f"q{i}"))
        self._fill("#queue-list", options)
        ol = self.query_one("#queue-list", NavList)
        if options and 0 <= self.queue.index < len(options):
            ol.highlighted = self.queue.index
        total = sum(s.duration for s in self.queue.songs)
        panel.border_subtitle = (
            f"{len(self.queue.songs)}♪ · {anim.fmt_time(total)}" if self.queue.songs else None
        )

    def action_enqueue(self, play_next: bool) -> None:
        focused = self.focused
        songs: list[Song] = []
        label = ""
        if focused is not None and focused.id == "pane3-list":
            ol = self.query_one("#pane3-list", NavList)
            if ol.highlighted is not None and ol.highlighted < len(self._songs):
                song = self._songs[ol.highlighted]
                songs, label = [song], song.title
        elif focused is not None and focused.id == "pane2-list":
            ol = self.query_one("#pane2-list", NavList)
            if ol.highlighted is not None and ol.highlighted < len(self._albums):
                album = self._albums[ol.highlighted]
                cached = self.dirs.read_cache(f"album-songs-{album.id}")
                songs = [Song.from_dict(s) for s in (cached or {}).get("songs", [])]
                label = album.name
                if not songs:
                    self._enqueue_album_async(album, play_next)
                    return
        if not songs:
            return
        if play_next:
            self.queue.add_next(songs)
        else:
            self.queue.add(songs)
        self._render_queue()
        self._persist_queue()
        self.notify(f"queued {'next: ' if play_next else ''}{label}", timeout=2)

    @work(group="mutate")
    async def _enqueue_album_async(self, album: Album, play_next: bool) -> None:
        try:
            songs = await self.client.get_album_songs(album.id)
        except Exception as e:
            self._connection_trouble(e)
            return
        self.dirs.write_cache(f"album-songs-{album.id}", {"songs": [s.to_dict() for s in songs]})
        if play_next:
            self.queue.add_next(songs)
        else:
            self.queue.add(songs)
        self._render_queue()
        self._persist_queue()
        self.notify(f"queued {'next: ' if play_next else ''}{album.name}", timeout=2)

    def action_queue_remove(self) -> None:
        focused = self.focused
        if focused is None or focused.id != "queue-list":
            return
        ol = self.query_one("#queue-list", NavList)
        if ol.highlighted is None:
            return
        was_current = ol.highlighted == self.queue.index
        self.queue.remove(ol.highlighted)
        if was_current:
            self._play_current()
        else:
            self._render_queue()
        self._persist_queue()

    def action_queue_clear(self) -> None:
        self.queue.clear()
        self.player.stop()
        now = self.query_one("#now", NowPlaying)
        now.set_song(None)
        now.set_playing(False)
        self._render_queue()
        self._persist_queue()
        self.notify("queue cleared", timeout=2)

    def _persist_queue(self, position: float | None = None) -> None:
        data = self.queue.to_dict()
        data["position"] = position if position is not None else (self.player.position if self.player else 0.0)
        self.dirs.write_cache("queue", data)

    # ── starring ──────────────────────────────────────────────────────
    def action_star(self) -> None:
        focused = self.focused
        if focused is None:
            return
        item = None
        kind = ""
        if focused.id == "pane3-list":
            ol = self.query_one("#pane3-list", NavList)
            if ol.highlighted is not None and ol.highlighted < len(self._songs):
                item, kind = self._songs[ol.highlighted], "song"
        elif focused.id == "pane2-list":
            ol = self.query_one("#pane2-list", NavList)
            if ol.highlighted is not None and ol.highlighted < len(self._albums):
                item, kind = self._albums[ol.highlighted], "album"
        elif focused.id == "pane1-list" and self.tab in ("library", "starred"):
            ol = self.query_one("#pane1-list", NavList)
            if ol.highlighted is not None and ol.highlighted < len(self._artists):
                item, kind = self._artists[ol.highlighted], "artist"
        elif focused.id == "queue-list":
            ol = self.query_one("#queue-list", NavList)
            if ol.highlighted is not None and ol.highlighted < len(self.queue.songs):
                item, kind = self.queue.songs[ol.highlighted], "song"
        if item is None:
            return
        item.starred = not item.starred  # optimistic — the cache IS the truth
        self._star(item.id, kind, item.starred)
        self._repaint_lists()
        current = self.queue.current
        if kind == "song" and current is not None and item.id == current.id:
            current.starred = item.starred
            self.query_one("#now", NowPlaying).song = current

    def _repaint_lists(self) -> None:
        if self.tab == "library":
            self._fill("#pane1-list", [self._artist_row(a) for a in self._artists])
        elif self.tab == "starred":
            self._apply_starred(self._artists, self._albums, self._songs)
            return
        self._fill("#pane2-list", [self._album_row(a) for a in self._albums])
        self._refresh_song_markers()

    @work(group="mutate")
    async def _star(self, item_id: str, kind: str, star: bool) -> None:
        self._mutations += 1
        try:
            await self.client.set_star(item_id, kind, star)
        except Exception as e:
            self.notify(f"couldn't {'star' if star else 'unstar'}: {e}", severity="warning")
        finally:
            self._mutations -= 1

    @work(group="mutate")
    async def _scrobble(self, song_id: str, submission: bool) -> None:
        try:
            await self.client.scrobble(song_id, submission)
        except Exception:
            pass  # scrobbling is best-effort

    # ── art ───────────────────────────────────────────────────────────
    @work(exclusive=True, group="art")
    async def _load_art(self, cover_id: str, key: str) -> None:
        panel = self.query_one("#art-panel", CoverArt)
        try:
            path = await self.client.cover_art(cover_id)
        except Exception:
            panel.placeholder()
            return
        panel.show(path, key)

    # ── search ────────────────────────────────────────────────────────
    def action_search(self) -> None:
        if self.client is None:
            return
        self.push_screen(SearchModal(), self._search_done)

    def _search_done(self, result) -> None:
        if not result:
            return
        if result[0] == "song":
            _, songs, index = result
            self._play_songs(songs, index)
        elif result[0] == "album":
            album = result[1]
            self._play_album(album)
            self.notify(f"playing {album.name}", timeout=2)
        elif result[0] == "artist":
            artist = result[1]
            self.action_set_tab("library")
            ol = self.query_one("#pane1-list", NavList)
            idx = next((i for i, a in enumerate(self._artists) if a.id == artist.id), None)
            if idx is not None:
                ol.highlighted = idx
                ol.focus()

    # ── misc actions ──────────────────────────────────────────────────
    def action_focus_panel(self, direction: int) -> None:
        ids = ["pane1-list", "pane2-list", "pane3-list", "queue-list"]
        if self.tab == "playlists":
            ids.remove("pane2-list")  # that panel is hidden on this tab
        lists = [self.query_one(f"#{i}", NavList) for i in ids]
        if not lists:
            return
        focused = self.focused
        try:
            i = lists.index(focused)
        except ValueError:
            i = 0 if direction > 0 else 1
            direction = 0 if direction > 0 else -1
        lists[(i + direction) % len(lists)].focus()

    def action_refresh(self) -> None:
        self._show_tab(initial=True)
        self.notify("refreshing", timeout=1.5)

    def _maybe_auto_refresh(self) -> None:
        if self.client is None or self._mutations > 0:
            return
        if self.screen is not self.screen_stack[0]:
            return  # modal open — don't yank state around underneath it
        self._show_tab(initial=True)

    def action_help(self) -> None:
        self.push_screen(HelpModal(HELP_SECTIONS, title="NaviTui · keys"))

    def on_kit_theme_changed(self) -> None:
        if not self.kit_theme_previewing:
            self.dirs.save_state({"theme": self.theme})
        self._render_tabs()
        self._render_status()
        if self.client is not None:
            self._repaint_lists()
            self._render_queue()

    def _persist_width(self, selector: str, width: int | None) -> None:
        widths = self.dirs.load_state().get("widths", {})
        if width is None:
            widths.pop(selector, None)
        else:
            widths[selector] = width
        self.dirs.save_state({"widths": widths})

    def _connection_trouble(self, error: Exception) -> None:
        if isinstance(error, SubsonicError):
            self.notify(f"server error: {error}", severity="error", timeout=6)
        else:
            self.notify("offline — showing cached library", severity="warning", timeout=4)

    async def action_quit(self) -> None:
        if self.player is not None:
            self._persist_queue()
            self.player.terminate()
        if self.client is not None:
            try:
                await self.client.close()
            except Exception:
                pass
        self.exit()


def main() -> None:
    NaviTuiApp().run()


if __name__ == "__main__":
    main()
