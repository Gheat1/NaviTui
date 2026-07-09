"""NaviTui — the app.

Songs-first: one sidebar of ways-to-list-tracks (views + playlists), one big
tracks pane, cover + queue on the right. No tabs, no album browsing — albums
and artists only exist inside search.

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
from ricekit.modals import HelpModal, PickerModal
from ricekit.storage import AppDirs
from ricekit.widgets import NavList, Splitter

from navitui import anim, artcolor, config as configmod, player as playermod
from navitui.api import SubsonicClient, SubsonicError
from navitui.art import CoverArt
from navitui.integrations import DiscordPresence, Notifier
from navitui.models import Album, Artist, Playlist, Song
from navitui.mpris import Mpris
from navitui.playqueue import PlayQueue
from navitui.screens import InputModal, LyricsModal, OnboardingScreen, SearchModal
from navitui.widgets import ClickList, Logo, NowPlaying, PAUSE_GLYPH, PLAY_GLYPH

# read once at import so the bindings table below can be built from it;
# remapping a key is an edit to player.toml + restart
CONFIG = configmod.load(AppDirs("navitui").config_file.parent)


def _kb(action_id: str, action: str, description: str = "", show: bool = False) -> Binding:
    return Binding(CONFIG["keybinds"][action_id], action, description, show=show)


VIEWS = [
    ("all-songs", "all tracks"),
    ("newest", "recently added"),
    ("recent", "recently played"),
    ("frequent", "most played"),
    ("starred", "starred"),
    ("shuffle-all", "shuffle everything"),
]
VIEW_LABELS = dict(VIEWS)

HELP_SECTIONS = [
    (
        "playback",
        [
            ("space", "play / pause"),
            ("enter / double-click", "play (track, view, playlist)"),
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
            ("a", "add track to queue"),
            ("A", "play track next"),
            ("x", "remove track (in queue panel)"),
            ("ctrl+↑ / ctrl+↓", "move track up / down"),
            ("X", "clear queue"),
            ("", "played tracks dim out — scroll up for history"),
        ],
    ),
    (
        "config & desktop",
        [
            ("N", "toggle desktop notifications"),
            ("", "media keys work via MPRIS (linux)"),
            ("", "~/.config/navitui/player.toml: keybinds,"),
            ("", "replaygain, gapless, discord presence"),
        ],
    ),
    (
        "library",
        [
            ("j / k / g / G", "move in lists"),
            ("h / l", "previous / next panel"),
            ("/", "search  (enter play · a queue · A play next)"),
            ("p", "add track to a playlist"),
            ("f", "star / unstar track"),
            ("1-5", "rate track (same digit again clears)"),
            ("e / E", "go to track's album / artist"),
            ("L", "lyrics"),
            ("S", "copy share link"),
            ("R", "refresh from server"),
        ],
    ),
    (
        "app",
        [
            ("t", "cycle kit themes"),
            ("T", "theme picker (live preview)"),
            ("z", "zen / now-playing splash"),
            ("?", "this help"),
            ("q", "quit"),
        ],
    ),
]


class NaviTuiApp(KitApp):
    TITLE = "NaviTui"

    BINDINGS = [
        _kb("play_pause", "play_pause", "play/pause", show=True),
        _kb("next_track", "next_track", "next", show=True),
        _kb("prev_track", "prev_track"),
        _kb("search", "search", "search", show=True),
        _kb("shuffle", "toggle_shuffle", "shuffle", show=True),
        _kb("repeat", "cycle_repeat", "repeat", show=True),
        _kb("seek_back", "seek(-5)"),
        _kb("seek_forward", "seek(5)"),
        _kb("seek_back_big", "seek(-30)"),
        _kb("seek_forward_big", "seek(30)"),
        _kb("volume_down", "volume(-5)"),
        _kb("volume_up", "volume(5)"),
        _kb("mute", "mute"),
        _kb("enqueue", "enqueue(False)"),
        _kb("play_next", "enqueue(True)"),
        _kb("queue_remove", "queue_remove"),
        _kb("queue_clear", "queue_clear"),
        _kb("queue_move_up", "queue_move(-1)"),
        _kb("queue_move_down", "queue_move(1)"),
        _kb("star", "star"),
        _kb("playlist_add", "playlist_add"),
        _kb("lyrics", "lyrics"),
        _kb("share", "share"),
        _kb("go_album", "go_album"),
        _kb("go_artist", "go_artist"),
        _kb("notifications", "toggle_notifications"),
        _kb("panel_prev", "focus_panel(-1)"),
        _kb("panel_next", "focus_panel(1)"),
        _kb("refresh", "refresh"),
        _kb("theme_cycle", "cycle_kit_theme", "theme", show=True),
        _kb("theme_pick", "change_theme"),
        _kb("zen", "toggle_zen", "zen", show=True),
        _kb("help", "help", "help", show=True),
        _kb("quit", "quit", "quit", show=True),
        # rating is fixed on the number row (press again to clear)
        *(Binding(str(n), f"rate({n})", show=False) for n in range(1, 6)),
    ]

    CSS = """
    #topbar { height: 1; padding: 0 1; }
    #topbar #status { width: 1fr; text-align: right; }

    #main { height: 1fr; }
    NavList { text-wrap: nowrap; text-overflow: ellipsis; }
    .panel { border: round $kit-border; }
    .panel:focus-within { border: round $kit-border-focus; }
    .panel NavList { height: 1fr; }
    #sidebar-panel { width: 26; }
    #tracks-panel { width: 1fr; }
    #side { width: 36; }
    #art-panel { height: 40%; min-height: 12; border: round $kit-border; }
    #queue-panel { height: 1fr; border: round $kit-border; }

    NowPlaying.playing { border: round $kit-border-alt; }

    #zen-info { display: none; }

    /* zen / now-playing splash: hide everything but a big centered cover,
       the track info line and the animated transport (all still driven by
       the one heartbeat — no extra timers). */
    .zen #sidebar-panel, .zen #split1, .zen #tracks-panel,
    .zen #split2, .zen #queue-panel { display: none; }
    .zen #side { width: 1fr; align: center middle; }
    .zen #art-panel {
        width: 60%; height: 1fr; max-width: 72;
        border: none; content-align: center middle;
    }
    .zen #zen-info { display: block; height: auto; margin: 1 0; }
    """

    def __init__(self, client: SubsonicClient | None = None, ao: str | None = None) -> None:
        super().__init__()
        self.dirs = AppDirs("navitui")
        self.client: SubsonicClient | None = client
        self._ao = ao
        self.queue = PlayQueue()
        self.player = None
        self.view: str = "all-songs"  # sidebar view id (or "pl:<id>", or "artist:<id>")
        self._songs: list[Song] = []  # what the tracks pane shows
        self._playlists: list[Playlist] = []
        # playback bookkeeping
        self._scrobbled = False
        self._end_failures = 0
        self._resume_position = 0.0
        self._mutations = 0
        self._last_persist = 0.0
        self._queue_scrolled_to = -2
        self._zen = False

    # ── layout ────────────────────────────────────────────────────────
    def compose(self):
        with Horizontal(id="topbar"):
            yield Logo(id="logo")
            yield Static(id="status")
        with Horizontal(id="main"):
            with Vertical(id="sidebar-panel", classes="panel"):
                yield ClickList(id="sidebar-list")
            yield Splitter("#sidebar-panel", on_resized=self._persist_width, id="split1")
            with Vertical(id="tracks-panel", classes="panel"):
                yield ClickList(id="tracks-list")
            yield Splitter("#side", invert=True, on_resized=self._persist_width, id="split2")
            with Vertical(id="side"):
                yield CoverArt(id="art-panel")
                yield Static(id="zen-info")
                with Vertical(id="queue-panel", classes="panel"):
                    yield ClickList(id="queue-list")
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

        self.query_one("#sidebar-panel").border_title = "tracks"
        self.query_one("#tracks-panel").border_title = "tracks"
        self.query_one("#art-panel", CoverArt).border_title = "cover"
        self.query_one("#queue-panel").border_title = "queue"
        saved_view = state.get("view", "all-songs")
        if saved_view in VIEW_LABELS or saved_view.startswith("pl:"):
            self.view = saved_view

        configmod.write_template(self.dirs.config_file.parent)
        self.notifier = Notifier(bool(CONFIG["notifications"]))
        self.discord = DiscordPresence(
            bool(CONFIG["discord_rich_presence"]), str(CONFIG["discord_app_id"])
        )
        self.mpris = Mpris()

        self.player = playermod.create_player(
            self._mpv_position,
            self._mpv_track_end,
            ao=self._ao,
            replaygain=str(CONFIG["replaygain"]),
            gapless=str(CONFIG["gapless"]),
        )
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
        cached = self.dirs.read_cache("playlists")
        if cached:
            self._playlists = [Playlist.from_dict(p) for p in cached.get("playlists", [])]
        self._render_sidebar()
        sidebar = self.query_one("#sidebar-list", ClickList)
        sidebar.focus()
        self._highlight_view(self.view)
        self._load_playlists()
        self.run_worker(self._start_mpris(), group="mpris")

    async def _start_mpris(self) -> None:
        # dbus-fast runs on our own event loop, so media-key controls can
        # call app actions directly — no thread marshalling
        controls = {
            "play_pause": self.action_play_pause,
            "play": lambda: (self.player.set_paused(False) if self.player.active else self.action_play_pause()),
            "pause": lambda: self.player.set_paused(True),
            "stop": self._external_stop,
            "next": self.action_next_track,
            "prev": self.action_prev_track,
            "seek": self.player.seek,
            "set_position": lambda s: self.player.seek_to(s / max(1.0, self.player.duration)),
        }
        if await self.mpris.start(controls):
            self._announce()

    def _external_stop(self) -> None:
        self.player.stop()
        now = self.query_one("#now", NowPlaying)
        now.set_playing(False)
        self._announce()

    def _announce(self, track_change: bool = False) -> None:
        """Fan the player state out to MPRIS, Discord and (on track change)
        a desktop notification."""
        active = bool(self.player and self.player.active)
        song = self.queue.current if active else None
        playing = active and not self.player.paused
        art = None
        if song is not None and song.cover_art and self.client is not None:
            art = self.client.cached_art(song.cover_art)
        self.mpris.update(
            song, playing,
            self.player.position if self.player else 0.0,
            self.player.volume if self.player else 100,
            str(art) if art else None,
        )
        self.discord.track(
            song, playing,
            self.player.position if self.player else 0.0,
            float(song.duration) if song else 0.0,
        )
        if track_change and song is not None:
            self.notifier.track(song, art)

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
            level = None
            if self.player is not None:
                now.set_playing(self.player.active and not self.player.paused)
                now.set_class(self.player.active, "playing")
                level = self.player.level
            now.tick(level)
            if self._zen:
                self._render_zen_info()  # follow track changes in the splash
            busy = any(
                not w.is_finished
                for w in self.workers
                if w.group in ("lib", "songs")
            )
            panel = self.query_one("#tracks-panel")
            if busy:
                panel.border_subtitle = f"{anim.spinner(int(now._tick))} refreshing"
            elif panel.border_subtitle and "refreshing" in panel.border_subtitle:
                count = self.query_one("#tracks-list", NavList).option_count
                panel.border_subtitle = str(count) if count else None
        except Exception:
            return  # shutdown race: the timer can fire while widgets unmount

    # ── sidebar ───────────────────────────────────────────────────────
    def _render_sidebar(self) -> None:
        ol = self.query_one("#sidebar-list", ClickList)
        highlighted_id = None
        if ol.highlighted is not None:
            highlighted_id = ol.get_option_at_index(ol.highlighted).id
        options: list[Option] = []
        options.append(Option(Text(" tracks", style=f"bold {palette.dim}"), disabled=True))
        for view_id, label in VIEWS:
            row = Text(no_wrap=True, overflow="ellipsis")
            glyph, color = ("", palette.peach) if view_id == "shuffle-all" else ("◍", palette.mauve)
            if view_id == "starred":
                glyph, color = icons.STAR, palette.yellow
            row.append(f"{glyph} ", style=color)
            row.append(label, style=palette.text)
            options.append(Option(row, id=view_id))
        options.append(Option(Text(" "), disabled=True))
        options.append(Option(Text(" playlists", style=f"bold {palette.dim}"), disabled=True))
        for p in self._playlists:
            row = Text(no_wrap=True, overflow="ellipsis")
            row.append(f"{icons.LIST} ", style=palette.lav)
            row.append(p.name, style=palette.text)
            row.append(f" {p.song_count}♪", style=palette.vfaint)
            options.append(Option(row, id=f"pl:{p.id}"))
        new_row = Text(no_wrap=True)
        new_row.append(f"{icons.PLUS} ", style=palette.green)
        new_row.append("new playlist", style=palette.sub)
        options.append(Option(new_row, id="pl-new"))

        had_focus = ol.has_focus
        ol.clear_options()
        ol.add_options(options)
        self._highlight_view(highlighted_id or self.view)
        if had_focus:
            ol.focus()

    def _highlight_view(self, view_id: str | None) -> None:
        if not view_id:
            return
        ol = self.query_one("#sidebar-list", ClickList)
        for i in range(ol.option_count):
            if ol.get_option_at_index(i).id == view_id:
                ol.highlighted = i
                return

    @on(OptionList.OptionHighlighted, "#sidebar-list")
    def _sidebar_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        oid = event.option.id
        if not oid or oid == "pl-new":
            return
        self.view = oid
        self.dirs.save_state({"view": oid})
        self._load_view(oid)

    @on(OptionList.OptionSelected, "#sidebar-list")
    def _sidebar_selected(self, event: OptionList.OptionSelected) -> None:
        oid = event.option.id
        if not oid:
            return
        if oid == "pl-new":
            self.push_screen(
                InputModal("new playlist", placeholder="name"), self._playlist_created_name
            )
        elif oid == "shuffle-all":
            self._shuffle_everything()
        elif self._songs:
            # enter on a view or playlist just plays it from the top
            self._play_songs(self._songs, 0)

    # ── loading songs into the tracks pane ────────────────────────────
    def _tracks_title(self, view_id: str) -> str:
        if view_id.startswith("pl:"):
            pid = view_id.split(":", 1)[1]
            playlist = next((p for p in self._playlists if p.id == pid), None)
            return playlist.name if playlist else "playlist"
        return VIEW_LABELS.get(view_id, "tracks")

    def _show_songs(self, songs: list[Song], title: str) -> None:
        self._songs = songs
        panel = self.query_one("#tracks-panel")
        panel.border_title = title
        self._fill("#tracks-list", [self._song_row(s) for s in songs], "#tracks-panel")

    @work(exclusive=True, group="songs")
    async def _load_view(self, view_id: str) -> None:
        await asyncio.sleep(0.12)  # superseded while the cursor is moving
        title = self._tracks_title(view_id)

        if view_id in ("all-songs", "shuffle-all"):
            cache_key, fetch = "all-songs", self.client.get_all_songs
        elif view_id in ("newest", "recent", "frequent"):
            cache_key = f"songview-{view_id}"

            async def fetch(v=view_id):
                return await self.client.get_songs_by_albums(v)
        elif view_id == "starred":
            cache_key = "starred-songs"

            async def fetch():
                return (await self.client.get_starred()).songs
        elif view_id.startswith("pl:"):
            pid = view_id.split(":", 1)[1]
            cache_key = f"playlist-songs-{pid}"

            async def fetch(p=pid):
                return await self.client.get_playlist_songs(p)
        else:
            return

        cached = self.dirs.read_cache(cache_key)
        if cached:
            self._show_songs([Song.from_dict(s) for s in cached.get("songs", [])], title)
        try:
            songs = await fetch()
        except Exception as e:
            self._connection_trouble(e)
            return
        self.dirs.write_cache(cache_key, {"songs": [s.to_dict() for s in songs]})
        if self.view == view_id:
            self._show_songs(songs, title)

    @work(exclusive=True, group="songs")
    async def _load_artist_songs(self, artist: Artist) -> None:
        """Ad-hoc view from search: every song by an artist, flattened."""
        title = f"artist · {artist.name}"
        self.view = f"artist:{artist.id}"
        self._highlight_view(None)
        cache_key = f"artist-songs-{artist.id}"
        cached = self.dirs.read_cache(cache_key)
        if cached:
            self._show_songs([Song.from_dict(s) for s in cached.get("songs", [])], title)
        try:
            albums = await self.client.get_artist_albums(artist.id)
            results = await asyncio.gather(
                *(self.client.get_album_songs(a.id) for a in albums),
                return_exceptions=True,
            )
        except Exception as e:
            self._connection_trouble(e)
            return
        songs: list[Song] = []
        for r in results:
            if isinstance(r, list):
                songs.extend(r)
        self.dirs.write_cache(cache_key, {"songs": [s.to_dict() for s in songs]})
        self._show_songs(songs, title)
        self.query_one("#tracks-list", ClickList).focus()

    @work(exclusive=True, group="lib")
    async def _load_playlists(self) -> None:
        try:
            playlists = await self.client.get_playlists()
        except Exception as e:
            self._connection_trouble(e)
            return
        self.dirs.write_cache("playlists", {"playlists": [p.to_dict() for p in playlists]})
        self._playlists = playlists
        self._render_sidebar()

    # ── row rendering ─────────────────────────────────────────────────
    def _song_row(self, s: Song) -> Option:
        current = self.queue.current
        is_current = current is not None and s.id == current.id
        row = Text(no_wrap=True, overflow="ellipsis")
        marker = anim.NOTE_FRAMES[0] if is_current else "·"
        row.append(f" {marker} ", style=palette.blue if is_current else palette.vfaint)
        row.append(s.title, style=f"bold {palette.blue}" if is_current else palette.text)
        if s.starred:
            row.append(f" {icons.STAR}", style=palette.yellow)
        if s.user_rating:
            row.append(f" {chr(0x2460 + s.user_rating - 1)}", style=palette.peach)  # ①-⑤
        row.append(f"  {s.artist}", style=palette.dim)
        row.append(f" · {anim.fmt_time(s.duration)}", style=palette.vfaint)
        return Option(row, id=s.id)

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

    # ── tracks pane ───────────────────────────────────────────────────
    @on(OptionList.OptionHighlighted, "#tracks-list")
    def _track_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if self.player is not None and self.player.active:
            return  # while playing, the cover belongs to the current song
        song = next((s for s in self._songs if s.id == event.option.id), None)
        if song is not None and song.cover_art:
            self._load_art(song.cover_art, f"song-{song.id}")

    @on(OptionList.OptionSelected, "#tracks-list")
    def _track_selected(self, event: OptionList.OptionSelected) -> None:
        idx = next((i for i, s in enumerate(self._songs) if s.id == event.option.id), None)
        if idx is not None:
            self._play_songs(self._songs, idx)

    @on(OptionList.OptionSelected, "#queue-list")
    def _queue_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.highlighted is not None:
            self.queue.jump(event.option_list.highlighted)
            self._play_current()

    # ── playback ──────────────────────────────────────────────────────
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

    def _play_songs(self, songs: list[Song], start: int) -> None:
        self.queue.set_songs(songs, start)
        self._play_current()

    def _play_current(self, resume_at: float = 0.0) -> None:
        song = self.queue.current
        now = self.query_one("#now", NowPlaying)
        if song is None:
            self.player.stop()
            now.set_song(None)
            self._tint_from_art(None)
            self._render_queue()
            return
        self.player.play(self.client.stream_url(song.id), start=resume_at)
        now.set_song(song)
        now.set_progress(resume_at, song.duration)
        self._scrobbled = False
        self._scrobble(song.id, False)
        if song.cover_art:
            self._load_art(song.cover_art, f"song-{song.id}")
        else:
            self._tint_from_art(None)
        self._render_queue()
        self._refresh_song_markers()
        self._persist_queue()
        self._announce(track_change=True)

    def _refresh_song_markers(self) -> None:
        """Re-render the tracks pane so the ♪ marker follows the player."""
        self._fill("#tracks-list", [self._song_row(s) for s in self._songs])

    def action_play_pause(self) -> None:
        if self.player.active:
            self.player.toggle_pause()
            self._announce()
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
        self._announce()

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
        self.mpris.set_position(position)
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
            self._announce()

    # ── queue ─────────────────────────────────────────────────────────
    def _render_queue(self) -> None:
        panel = self.query_one("#queue-panel")
        options = []
        for i, song in enumerate(self.queue.songs):
            row = Text(no_wrap=True, overflow="ellipsis")
            if i < self.queue.index:
                # already played: dim it way down, scroll up to revisit
                row.append(f"{i + 1:>2d} ", style=palette.vfaint)
                row.append(song.title, style=palette.faint)
                row.append(f"  {song.artist}", style=palette.vfaint)
            elif i == self.queue.index:
                glyph = PLAY_GLYPH if (self.player and self.player.active and not self.player.paused) else PAUSE_GLYPH
                row.append(f"{glyph} ", style=palette.green)
                row.append(song.title, style=f"bold {palette.blue}")
                row.append(f"  {song.artist}", style=palette.dim)
            else:
                row.append(f"{i + 1:>2d} ", style=palette.vfaint)
                row.append(song.title, style=palette.text)
                row.append(f"  {song.artist}", style=palette.dim)
            options.append(Option(row, id=f"q{i}"))
        self._fill("#queue-list", options)
        ol = self.query_one("#queue-list", NavList)
        if options and 0 <= self.queue.index < len(options):
            ol.highlighted = self.queue.index
            # pin the current track to the top so the panel reads "up next";
            # only when the track changes, so manual scrollback isn't fought
            if self.queue.index != self._queue_scrolled_to:
                self._queue_scrolled_to = self.queue.index
                index = self.queue.index
                self.call_after_refresh(
                    lambda: ol.scroll_to(y=index, animate=False)
                )
        upcoming = self.queue.songs[self.queue.index + 1 :] if self.queue.index >= 0 else self.queue.songs
        remaining = sum(s.duration for s in upcoming)
        panel.border_subtitle = (
            f"{len(upcoming)}♪ up next · {anim.fmt_time(remaining)}" if self.queue.songs else None
        )

    def action_enqueue(self, play_next: bool) -> None:
        focused = self.focused
        if focused is None or focused.id != "tracks-list":
            return
        ol = self.query_one("#tracks-list", NavList)
        if ol.highlighted is None or ol.highlighted >= len(self._songs):
            return
        song = self._songs[ol.highlighted]
        if play_next:
            self.queue.add_next([song])
        else:
            self.queue.add([song])
        self._render_queue()
        self._persist_queue()
        self.notify(f"queued {'next: ' if play_next else ''}{song.title}", timeout=2)

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

    # ── playlists ─────────────────────────────────────────────────────
    def _highlighted_song(self) -> Song | None:
        focused = self.focused
        if focused is not None and focused.id == "tracks-list":
            ol = self.query_one("#tracks-list", NavList)
            if ol.highlighted is not None and ol.highlighted < len(self._songs):
                return self._songs[ol.highlighted]
        elif focused is not None and focused.id == "queue-list":
            ol = self.query_one("#queue-list", NavList)
            if ol.highlighted is not None and ol.highlighted < len(self.queue.songs):
                return self.queue.songs[ol.highlighted]
        return None

    def action_playlist_add(self) -> None:
        song = self._highlighted_song()
        if song is None:
            self.notify("highlight a track first (tracks or queue panel)", timeout=3)
            return
        options = [
            Option(Text(f" {icons.LIST} {p.name}", style=palette.text), id=f"pl:{p.id}")
            for p in self._playlists
        ]
        options.append(Option(Text(f" {icons.PLUS} new playlist…", style=palette.sub), id="pl-new"))

        def picked(choice: str | None) -> None:
            if not choice:
                return
            if choice == "pl-new":
                self.push_screen(
                    InputModal("new playlist", placeholder="name"),
                    lambda name: self._playlist_create(name, song) if name else None,
                )
            else:
                pid = choice.split(":", 1)[1]
                self._playlist_append(pid, song)

        self.push_screen(PickerModal(f"add “{song.title}” to…", options), picked)

    def _playlist_created_name(self, name: str | None) -> None:
        if name:
            self._playlist_create(name, None)

    @work(group="mutate")
    async def _playlist_create(self, name: str, song: Song | None) -> None:
        self._mutations += 1
        try:
            await self.client.create_playlist(name, [song.id] if song else [])
        except Exception as e:
            self.notify(f"couldn't create playlist: {e}", severity="error", timeout=5)
            return
        finally:
            self._mutations -= 1
        self.notify(
            f"created “{name}”" + (f" with {song.title}" if song else ""), timeout=3
        )
        self._load_playlists()

    @work(group="mutate")
    async def _playlist_append(self, playlist_id: str, song: Song) -> None:
        self._mutations += 1
        try:
            await self.client.add_to_playlist(playlist_id, [song.id])
        except Exception as e:
            self.notify(f"couldn't add to playlist: {e}", severity="error", timeout=5)
            return
        finally:
            self._mutations -= 1
        playlist = next((p for p in self._playlists if p.id == playlist_id), None)
        self.notify(f"added to “{playlist.name if playlist else 'playlist'}”", timeout=3)
        # the playlist's cached songs are stale now
        try:
            (self.dirs.cache_dir / f"playlist-songs-{playlist_id}.json").unlink()
        except OSError:
            pass
        self._load_playlists()

    # ── track extras: rating, lyrics, share, go-to ────────────────────
    def _target_song(self) -> Song | None:
        """The song an action applies to: the highlighted one if a list is
        focused, else whatever is playing."""
        return self._highlighted_song() or self.queue.current

    def action_rate(self, rating: int) -> None:
        song = self._target_song()
        if song is None:
            return
        new = 0 if song.user_rating == rating else rating  # same digit clears
        song.user_rating = new
        self._rate(song.id, new)
        self._refresh_song_markers()
        self.notify(f"rating: {'—' if new == 0 else '★' * new}", timeout=1.5)

    @work(group="mutate")
    async def _rate(self, song_id: str, rating: int) -> None:
        self._mutations += 1
        try:
            await self.client.set_rating(song_id, rating)
        except Exception as e:
            self.notify(f"couldn't set rating: {e}", severity="warning")
        finally:
            self._mutations -= 1

    def action_lyrics(self) -> None:
        song = self._target_song()
        if song is None:
            self.notify("nothing to look up", timeout=2)
            return
        self._fetch_lyrics(song)

    @work(exclusive=True, group="lyrics")
    async def _fetch_lyrics(self, song: Song) -> None:
        try:
            text = await self.client.get_lyrics(song.artist, song.title)
        except Exception:
            text = ""
        if not text.strip():
            self.notify(f"no lyrics found for {song.title}", timeout=3)
            return
        self.push_screen(LyricsModal(f"{song.title} — {song.artist}", text))

    def action_share(self) -> None:
        song = self._target_song()
        if song is None:
            return
        self._share(song)

    @work(group="mutate")
    async def _share(self, song: Song) -> None:
        try:
            url = await self.client.create_share(song.id)
        except Exception as e:
            self.notify(f"couldn't create share: {e}", severity="warning", timeout=5)
            return
        self.copy_to_clipboard(url)
        self.notify(f"share link copied · {url}", timeout=5)

    def action_go_album(self) -> None:
        song = self._target_song()
        if song is None or not song.album_id:
            return
        self._load_album_adhoc(song)

    @work(exclusive=True, group="songs")
    async def _load_album_adhoc(self, song: Song) -> None:
        title = f"album · {song.album}"
        self.view = f"album:{song.album_id}"
        self._highlight_view(None)
        cache_key = f"album-songs-{song.album_id}"
        cached = self.dirs.read_cache(cache_key)
        if cached:
            self._show_songs([Song.from_dict(s) for s in cached.get("songs", [])], title)
        try:
            songs = await self.client.get_album_songs(song.album_id)
        except Exception as e:
            self._connection_trouble(e)
            return
        self.dirs.write_cache(cache_key, {"songs": [s.to_dict() for s in songs]})
        self._show_songs(songs, title)
        ol = self.query_one("#tracks-list", ClickList)
        idx = next((i for i, s in enumerate(songs) if s.id == song.id), None)
        if idx is not None:
            ol.highlighted = idx
        ol.focus()

    def action_go_artist(self) -> None:
        song = self._target_song()
        if song is None or not song.artist_id:
            return
        self._load_artist_songs(Artist(id=song.artist_id, name=song.artist))

    def action_toggle_notifications(self) -> None:
        on_now = self.notifier.toggle()
        self.notify(f"notifications {'on' if on_now else 'off'}", timeout=2)

    def action_queue_move(self, delta: int) -> None:
        focused = self.focused
        if focused is None or focused.id != "queue-list":
            return
        ol = self.query_one("#queue-list", NavList)
        if ol.highlighted is None:
            return
        new = self.queue.move(ol.highlighted, delta)
        if new is None:
            return
        self._render_queue()
        ol.highlighted = new
        self._persist_queue()

    # ── starring ──────────────────────────────────────────────────────
    def action_star(self) -> None:
        song = self._highlighted_song()
        if song is None:
            return
        song.starred = not song.starred  # optimistic — the cache IS the truth
        self._star(song.id, "song", song.starred)
        self._refresh_song_markers()
        self._render_queue()
        current = self.queue.current
        if current is not None and song.id == current.id:
            current.starred = song.starred
            self.query_one("#now", NowPlaying).song = current

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
            self._tint_from_art(None)
            return
        panel.show(path, key)
        self._tint_from_art(path)

    def _tint_from_art(self, path: Path | None) -> None:
        """Live-tint the chrome with the cover's dominant color (or clear
        it). Off unless enabled + truecolor; any failure leaves it untinted."""
        if not CONFIG["art_theming"] or path is None:
            artcolor.set_tint(None)
            return
        try:
            artcolor.set_tint(artcolor.extract_vibrant(path))
        except Exception:
            artcolor.set_tint(None)

    # ── search ────────────────────────────────────────────────────────
    def action_search(self) -> None:
        if self.client is None:
            return
        self.push_screen(SearchModal(), self._search_done)

    def _search_done(self, result) -> None:
        if not result:
            return
        kind = result[0]
        if kind == "song":
            _, songs, index = result
            self._play_songs(songs, index)
        elif kind == "song-queue":
            _, song, play_next = result
            if play_next:
                self.queue.add_next([song])
            else:
                self.queue.add([song])
            self._render_queue()
            self._persist_queue()
            self.notify(f"queued {'next: ' if play_next else ''}{song.title}", timeout=2)
        elif kind == "album":
            self._enqueue_album(result[1])
        elif kind == "artist":
            self._load_artist_songs(result[1])

    @work(group="mutate")
    async def _enqueue_album(self, album: Album) -> None:
        try:
            songs = await self.client.get_album_songs(album.id)
        except Exception as e:
            self._connection_trouble(e)
            return
        self.queue.add(songs)
        self._render_queue()
        self._persist_queue()
        self.notify(f"queued album: {album.name}", timeout=3)

    # ── misc actions ──────────────────────────────────────────────────
    def action_focus_panel(self, direction: int) -> None:
        lists = [
            self.query_one("#sidebar-list", NavList),
            self.query_one("#tracks-list", NavList),
            self.query_one("#queue-list", NavList),
        ]
        focused = self.focused
        try:
            i = lists.index(focused)
        except ValueError:
            i = 0 if direction > 0 else 1
            direction = 0 if direction > 0 else -1
        lists[(i + direction) % len(lists)].focus()

    def action_refresh(self) -> None:
        self._load_playlists()
        if not self.view.startswith(("artist:", "album:")):
            self._load_view(self.view)  # ad-hoc views have no sidebar entry
        self.notify("refreshing", timeout=1.5)

    def _maybe_auto_refresh(self) -> None:
        if self.client is None or self._mutations > 0:
            return
        if self.screen is not self.screen_stack[0]:
            return  # modal open — don't yank state around underneath it
        self._load_playlists()
        if not self.view.startswith(("artist:", "album:")):
            self._load_view(self.view)

    def action_help(self) -> None:
        self.push_screen(HelpModal(HELP_SECTIONS, title="NaviTui · keys"))

    # ── zen / now-playing splash ──────────────────────────────────────
    def action_toggle_zen(self) -> None:
        self._zen = not self._zen
        self.set_class(self._zen, "zen")
        if self._zen:
            self._render_zen_info()
        else:
            # restore focus to a sensible list when the panels come back
            self.query_one("#tracks-list", ClickList).focus()

    def _render_zen_info(self) -> None:
        """The big title/artist/album block under the cover in zen mode."""
        song = self.queue.current
        info = self.query_one("#zen-info", Static)
        t = Text(justify="center")
        if song is None:
            t.append("nothing playing", style=palette.dim)
        else:
            t.append(song.title, style=f"bold {palette.text}")
            if song.starred:
                t.append(f" {icons.STAR}", style=palette.yellow)
            t.append(f"\n{song.artist}", style=palette.sub)
            if song.album:
                t.append(f"\n{song.album}", style=palette.dim)
        info.update(t)

    def on_kit_theme_changed(self) -> None:
        if not self.kit_theme_previewing:
            self.dirs.save_state({"theme": self.theme})
        # the palette was just rebuilt for the new theme — re-assert the
        # album tint on top (a no-op under the ANSI `system` theme)
        artcolor.reapply()
        self._render_status()
        if self.client is not None:
            self._render_sidebar()
            self._refresh_song_markers()
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
        self.mpris.stop()
        self.discord.stop()
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
