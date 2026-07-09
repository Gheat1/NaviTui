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
import time

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

from navitui import anim, artcolor, card, config as configmod, player as playermod
from navitui.api import SubsonicClient, SubsonicError
from navitui.art import CoverArt
from navitui.integrations import DiscordPresence, ListenBrainz, Notifier
from navitui.models import Album, Artist, Bookmark, Genre, Playlist, PodcastChannel, Song
from navitui.mpris import Mpris
from navitui import mutations as mutations_mod
from navitui.mutations import MutationQueue
from navitui.palette import NaviTuiCommands
from navitui.playqueue import PlayQueue
from navitui.remote import Remote, build_snapshot
from navitui.screens import InputModal, LyricsModal, OnboardingScreen, SearchModal, StatsModal
from navitui.stats import StatsStore
from navitui import stats as statsmod
from navitui.widgets import ClickList, Logo, NowPlaying, PAUSE_GLYPH, PLAY_GLYPH

# read once at import so the bindings table below can be built from it;
# remapping a key is an edit to player.toml + restart
CONFIG = configmod.load(AppDirs("navitui").config_file.parent)

# nf-fa-bookmark — as a \uXXXX escape (raw PUA glyphs don't survive patching)
BOOKMARK_GLYPH = "\uf02e"

# nerd-font glyphs kept as \uXXXX escapes (raw PUA does not survive patch
# tooling): nf-fa-podcast + nf-fa-broadcast_tower for the two new sections
PODCAST_GLYPH = "\uf2ce"
RADIO_GLYPH = "\uf519"


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

# playback speeds cycled by the speed action — 1.0 first so a fresh press
# reads as "no change yet" only after it wraps back
SPEED_STEPS = [1.0, 1.25, 1.5, 1.75, 2.0, 0.75]
# sleep-timer presets: minutes, or 0 for off, or -1 for "stop at end of the
# current track". Cycled by the sleep action; index 0 is always off.
SLEEP_PRESETS = [0, 15, 30, 45, 60, -1]

# Runtime streaming-quality presets (label, kbps cap, format). 0 kbps + "" is
# original/untranscoded. Cycled with the quality keybind; the chosen cap only
# affects streams started afterwards — a playing track keeps its URL.
QUALITY_PRESETS = [
    ("original", 0, ""),
    ("320 kbps", 320, "mp3"),
    ("192 kbps", 192, "mp3"),
    ("96 kbps", 96, "opus"),
]


# Keys and descriptions are kept short on purpose: ricekit's HelpModal is a
# fixed-width box that pads the key column to the widest key, so a long key or
# description wraps and breaks the columns. Widest key here is "[count] j/k".
HELP_SECTIONS = [
    (
        "playback",
        [
            ("space", "play / pause"),
            ("enter", "play track / view / playlist"),
            ("n / b", "next / previous track"),
            ("← / →", "seek 5s  (shift: 30s)"),
            ("- / +", "volume down / up"),
            ("m", "mute"),
            (">", "cycle playback speed"),
            ("<", "sleep timer  (off→15→…→end)"),
            ("s", "toggle shuffle"),
            ("r", "cycle repeat  (off→all→one)"),
            ("i", "start radio from track / artist"),
            ("I", "toggle endless autoplay"),
        ],
    ),
    (
        "queue",
        [
            ("a", "add to queue"),
            ("A", "play next"),
            ("x", "remove (in queue panel)"),
            ("ctrl+↑/↓", "move track up / down"),
            ("X", "clear queue"),
            ("ctrl+s", "save queue as a playlist"),
            ("", "played tracks dim — scroll up"),
        ],
    ),
    (
        "playlists",
        [
            ("p", "add track to a playlist"),
            ("P", "remove track from playlist"),
            ("shift+↑/↓", "reorder in playlist"),
            ("ctrl+r", "rename playlist"),
            ("ctrl+x", "delete playlist"),
            ("", "act on the open pl: view"),
        ],
    ),
    (
        "config & desktop",
        [
            ("N", "toggle notifications"),
            ("J", "jukebox (play on server)"),
            ("", "media keys via MPRIS (linux)"),
            ("", "config: player.toml — keybinds,"),
            ("", "replaygain, gapless, bitrate…"),
        ],
    ),
    (
        "library",
        [
            ("j/k/g/G", "move in lists"),
            ("[count] j/k", "repeat motion (3j = down 3)"),
            ("h / l", "previous / next panel"),
            ("/", "search"),
            ("\\", "filter pane (type to narrow)"),
            ("v", "select mode (space toggles)"),
            ("", "then a/A/p/f/d act on selection"),
            ("f", "star / unstar"),
            ("d / D", "download track / view"),
            ("ctrl+d", "download whole library"),
            ("O", "offline mode"),
            ("Q", "cycle stream quality"),
            ("1-5", "rate track (repeat clears)"),
            ("e / E", "go to album / artist"),
            ("y", "browse by genre"),
            ("w / W", "bookmark pos / jump to it"),
            ("L", "lyrics"),
            ("S", "copy share link"),
            ("C", "export now-playing card"),
            ("R", "refresh from server"),
        ],
    ),
    (
        "app",
        [
            ("t", "cycle kit themes"),
            ("T", "theme picker (live preview)"),
            ("ctrl+w", "listening stats / wrapped"),
            ("z", "zen / now-playing splash"),
            ("ctrl+p", "command palette (search)"),
            ("?", "this help"),
            ("q", "quit"),
        ],
    ),
]


class NaviTuiApp(KitApp):
    TITLE = "NaviTui"

    # our verb list on top of Textual's built-in system commands (never
    # instead of them), so the palette lists NaviTui's actions and the
    # standard theme/screenshot/quit system entries together
    COMMANDS = KitApp.COMMANDS | {NaviTuiCommands}

    BINDINGS = [
        _kb("play_pause", "play_pause", "play/pause", show=True),
        _kb("next_track", "next_track", "next", show=True),
        _kb("prev_track", "prev_track"),
        _kb("search", "search", "search", show=True),
        _kb("shuffle", "toggle_shuffle", "shuffle", show=True),
        _kb("repeat", "cycle_repeat", "repeat", show=True),
        _kb("filter", "filter"),
        _kb("seek_back", "seek(-5)"),
        _kb("seek_forward", "seek(5)"),
        _kb("seek_back_big", "seek(-30)"),
        _kb("seek_forward_big", "seek(30)"),
        _kb("volume_down", "volume(-5)"),
        _kb("volume_up", "volume(5)"),
        _kb("mute", "mute"),
        _kb("speed", "cycle_speed"),
        _kb("sleep_timer", "cycle_sleep"),
        _kb("enqueue", "enqueue(False)"),
        _kb("play_next", "enqueue(True)"),
        _kb("queue_remove", "queue_remove"),
        _kb("queue_clear", "queue_clear"),
        _kb("queue_move_up", "queue_move(-1)"),
        _kb("queue_move_down", "queue_move(1)"),
        _kb("star", "star"),
        _kb("select_mode", "toggle_select_mode"),
        _kb("start_radio", "start_radio"),
        _kb("radio_toggle", "toggle_radio"),
        _kb("download", "download"),
        _kb("download_view", "download_view"),
        _kb("download_all", "download_all"),
        _kb("offline_toggle", "toggle_offline"),
        _kb("quality_cycle", "cycle_quality"),
        _kb("jukebox_toggle", "toggle_jukebox"),
        _kb("playlist_add", "playlist_add"),
        _kb("playlist_remove", "playlist_remove"),
        _kb("playlist_move_up", "playlist_move(-1)"),
        _kb("playlist_move_down", "playlist_move(1)"),
        _kb("playlist_rename", "playlist_rename"),
        _kb("playlist_delete", "playlist_delete"),
        _kb("queue_save", "queue_save"),
        _kb("lyrics", "lyrics"),
        _kb("stats", "stats"),
        _kb("share", "share"),
        _kb("export_card", "export_card"),
        _kb("go_album", "go_album"),
        _kb("go_artist", "go_artist"),
        _kb("genres", "pick_genre"),
        _kb("bookmark", "bookmark"),
        _kb("bookmarks", "pick_bookmark"),
        _kb("notifications", "toggle_notifications"),
        _kb("panel_prev", "focus_panel(-1)"),
        _kb("panel_next", "focus_panel(1)"),
        _kb("refresh", "refresh"),
        _kb("theme_cycle", "cycle_kit_theme", "theme", show=True),
        _kb("theme_pick", "change_theme"),
        _kb("zen", "toggle_zen", "zen", show=True),
        # explicit so it's remappable via player.toml; Textual would otherwise
        # auto-bind ctrl+p. Naming the binding `command_palette` also stops the
        # auto-bind from doubling up.
        _kb("command_palette", "command_palette", "palette"),
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
        # pending library writes to replay when the server comes back (or when
        # offline mode is switched off); survives restart via the JSON cache
        self.mutations = MutationQueue(
            lambda: self.dirs.read_cache(mutations_mod.CACHE_KEY),
            lambda data: self.dirs.write_cache(mutations_mod.CACHE_KEY, data),
        )
        self.client: SubsonicClient | None = client
        self._ao = ao
        # local, offline listening stats — one JSONL append per confirmed play
        self.stats = StatsStore(self.dirs.cache_dir)
        self.queue = PlayQueue()
        self.player = None
        self.view: str = "all-songs"  # sidebar view id (or "pl:<id>", or "artist:<id>")
        self._songs: list[Song] = []  # the full tracks-pane model
        self._playlists: list[Playlist] = []
        self._podcasts: list[tuple[PodcastChannel, list[Song]]] = []  # channels + episodes
        self._stations: list[Song] = []  # internet-radio stations as playable rows
        # type-to-filter: an explicit mode over the tracks pane. `_filtering`
        # captures keys in on_key so bare typing never fires a global bind;
        # `_filtered` is the derived view the list renders while active, so
        # play/enqueue/star still map to the right Song.
        self._filtering = False
        self._filter_query = ""
        self._filtered: list[Song] = []
        # multi-select: a set of song ids the bulk actions operate on. An
        # explicit `_select_mode` (toggled with `v`) turns `space` into
        # toggle-current-and-advance over the tracks pane; the selection is
        # scoped to the tracks pane and cleared whenever its rows change.
        self._select_mode = False
        self._selected: set[str] = set()
        # vim repeat count: digits armed by the previous keystroke, consumed by
        # the next motion (see _handle_count). Never spans more than the very
        # next key — no timer keeps it alive.
        self._count = ""
        # playback bookkeeping
        self._scrobbled = False
        self._end_failures = 0
        self._resume_position = 0.0
        # sleep timer: an index into SLEEP_PRESETS. _sleep_deadline is a
        # monotonic timestamp (checked inside the heartbeat, no extra timer);
        # None means "off" or the special "stop at end of current track" mode.
        self._sleep_idx = 0
        self._sleep_deadline: float | None = None
        self._mutations = 0
        self._last_persist = 0.0
        self._queue_scrolled_to = -2
        self._zen = False
        self._offline = False  # play/browse only what's pinned; skip the network
        self._jukebox = False  # play on the server's audio out, not this machine
        self._radio = False  # endless radio: refill the queue when it drains
        self._radio_filling = False  # guard against a runaway refill loop
        self._dl_total = 0
        self._dl_done = 0
        self._dl_failed = 0
        self._crossfade = max(0.0, float(CONFIG["crossfade"]))  # soft-fade seconds
        self._fade_base: int | None = None  # user volume captured across an active fade
        self._prefetched: str | None = None  # song id we last warmed, to dedup

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
        if (
            saved_view in VIEW_LABELS
            or saved_view.startswith(("pl:", "podcast:"))
            or saved_view == "radio"
        ):
            self.view = saved_view
        self._offline = bool(state.get("offline", False))
        self._radio = bool(state.get("radio", False))
        # jukebox mode: config default, overridable by the last runtime toggle
        self._jukebox = bool(state.get("jukebox", CONFIG["jukebox"]))

        configmod.write_template(self.dirs.config_file.parent)
        self.notifier = Notifier(bool(CONFIG["notifications"]))
        self.discord = DiscordPresence(
            bool(CONFIG["discord_rich_presence"]), str(CONFIG["discord_app_id"])
        )
        self.listenbrainz = ListenBrainz(str(CONFIG["listenbrainz_token"]))
        self.mpris = Mpris()
        self.remote = Remote()

        # start on the local engine; jukebox needs a client, so `_start` swaps
        # to it once one exists (keeps mpv the safe default through onboarding)
        self.player = self._make_player(jukebox=False)
        self.player.set_volume(int(state.get("volume", 80)))
        now = self.query_one("#now", NowPlaying)
        now.volume = self.player.volume
        now.speed = self.player.set_speed(float(state.get("speed", 1.0)))

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

        # local control API — transport + state work without a server
        # connection, so start it here (not in _start) regardless of onboarding
        self.run_worker(self._start_remote(), group="remote")

        if not playermod.MPV_AVAILABLE:
            self.notify(playermod.INSTALL_HINTS, severity="warning", timeout=15)

        if self.client is None:
            config = self.dirs.load_config()
            if all(config.get(k) for k in ("server", "username", "token", "salt")):
                self.client = SubsonicClient(
                    config["server"], config["username"], config["token"], config["salt"],
                    art_dir=self.dirs.cache_dir / "art",
                    audio_dir=self.dirs.cache_dir / "audio",
                    max_bitrate=int(CONFIG["max_bitrate"]),
                    stream_format=str(CONFIG["stream_format"]),
                )
            else:
                self.push_screen(
                    OnboardingScreen(config.get("server", ""), config.get("username", "")),
                    self._onboarded,
                )
                return
        self._start()

    # ── engine selection (local mpv ⇄ server jukebox) ─────────────────
    def _make_player(self, jukebox: bool):
        """Build a player for the requested mode. Jukebox drives the server's
        own audio out via the same duck-typed interface as the local mpv
        player; if it can't be built (no client yet) we fall back to local so
        mpv always stays the safe default."""
        if jukebox and self.client is not None:
            return playermod.create_player(
                self._mpv_position,
                self._mpv_track_end,
                jukebox=True,
                client=self.client,
                loop=self._loop,
                on_unsupported=self._jukebox_unsupported,
            )
        return playermod.create_player(
            self._mpv_position,
            self._mpv_track_end,
            ao=self._ao,
            replaygain=str(CONFIG["replaygain"]),
            gapless=str(CONFIG["gapless"]),
        )

    def _switch_player(self, jukebox: bool, announce: bool = True) -> None:
        """Tear down the current engine and stand up the other, preserving
        volume and (re)starting the current track where it left off."""
        old = self.player
        volume = old.volume if old is not None else 80
        position = old.position if old is not None else 0.0
        was_active = bool(old is not None and old.active)
        if old is not None:
            old.terminate()
        self.player = self._make_player(jukebox=jukebox)
        self._jukebox = isinstance(self.player, self._jukebox_type())
        self.player.set_volume(volume)
        self.query_one("#now", NowPlaying).volume = self.player.volume
        if was_active and self.queue.current is not None:
            self._play_current(resume_at=position)
        if announce:
            if self._jukebox:
                self.notify("jukebox mode — playing on the server", timeout=3)
            else:
                self.notify("local playback", timeout=2)

    @staticmethod
    def _jukebox_type():
        from navitui.jukebox import JukeboxPlayer

        return JukeboxPlayer

    def _jukebox_unsupported(self, detail: str) -> None:
        """The server refused a jukebox command (no permission / unsupported).
        Fall back to local playback so music never just stops."""
        if not self._jukebox:
            return
        self._jukebox = False
        self.dirs.save_state({"jukebox": False})
        self.notify(
            f"jukebox unavailable ({detail}) — using local playback",
            severity="warning",
            timeout=6,
        )
        self._switch_player(jukebox=False, announce=False)

    def action_toggle_jukebox(self) -> None:
        if self.client is None:
            return
        target = not self._jukebox
        self.dirs.save_state({"jukebox": target})
        self._switch_player(jukebox=target)

    def _onboarded(self, config: dict | None) -> None:
        if not config:
            return
        self._save_secrets(config)
        self.client = SubsonicClient(
            config["server"], config["username"], config["token"], config["salt"],
            art_dir=self.dirs.cache_dir / "art",
            audio_dir=self.dirs.cache_dir / "audio",
            max_bitrate=int(CONFIG["max_bitrate"]),
            stream_format=str(CONFIG["stream_format"]),
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
        self._restore_podcasts_radio()
        self._render_sidebar()
        sidebar = self.query_one("#sidebar-list", ClickList)
        sidebar.focus()
        self._highlight_view(self.view)
        self._load_playlists()
        self._load_podcasts_radio()
        self._flush_mutations()  # drain anything parked from a previous session
        self.run_worker(self._start_mpris(), group="mpris")
        # now that a client exists, honor jukebox mode (config/persisted). The
        # queue restored quietly, so don't auto-play — just swap the engine.
        if self._jukebox and not isinstance(self.player, self._jukebox_type()):
            self.player = self._make_player(jukebox=True)
            self.player.set_volume(self.query_one("#now", NowPlaying).volume)
            self._jukebox = isinstance(self.player, self._jukebox_type())

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
        # notification action buttons ride the same event loop: an invoked
        # button calls straight back into an app action (falls back to a
        # buttonless notification when the dbus service isn't reachable).
        await self.notifier.start(
            {
                "previous": self.action_prev_track,
                "play-pause": self.action_play_pause,
                "next": self.action_next_track,
            }
        )

    async def _start_remote(self) -> None:
        # asyncio server on our own loop (like mpris): every handler drives an
        # existing app action. Handlers take an args dict, return a dict or
        # None. Wrapped so a startup failure can never stop the app.
        p = self.player

        def _seek(a: dict) -> None:
            if "to" in a:
                self.player.seek_to((float(a["to"]) / max(1.0, self.player.duration)))
            else:
                self.action_seek(float(a.get("delta", 0)))

        def _volume(a: dict) -> dict:
            if "set" in a:
                self.set_volume_fraction(max(0, min(130, int(a["set"]))) / 100)
            else:
                self.action_volume(int(a.get("delta", 0)))
            return {"volume": self.player.volume}

        controls = {
            "play_pause": lambda a: self.action_play_pause(),
            "play": lambda a: (p.set_paused(False) if p.active else self.action_play_pause()),
            "pause": lambda a: p.set_paused(True) or self._announce(),
            "stop": lambda a: self._external_stop(),
            "next": lambda a: self.action_next_track(),
            "prev": lambda a: self.action_prev_track(),
            "seek": _seek,
            "volume": _volume,
            "mute": lambda a: self.action_mute(),
            "shuffle": lambda a: self.action_toggle_shuffle(),
            "repeat": lambda a: self.action_cycle_repeat(),
            "search": self._remote_search,
            "enqueue": self._remote_enqueue,
        }
        try:
            ok = await self.remote.start(
                controls,
                self._remote_snapshot,
                self.dirs.cache_dir,
                token=str(CONFIG["remote_token"]),
                enabled=bool(CONFIG["remote_control"]),
            )
        except Exception:
            ok = False
        if ok:
            self.remote.publish(self._remote_snapshot())

    def _remote_snapshot(self) -> dict:
        active = bool(self.player and self.player.active)
        playing = active and not (self.player and self.player.paused)
        return build_snapshot(
            self.queue.current if active else None,
            self.queue.songs,
            self.queue.index,
            self.player.position if self.player else 0.0,
            self.player.volume if self.player else 0,
            bool(self.player and self.player.muted),
            playing,
            active,
            self.queue.shuffle,
            self.queue.repeat.value,
        )

    async def _remote_search(self, a: dict) -> dict:
        if self.client is None:
            return {"songs": [], "albums": [], "artists": []}
        res = await self.client.search(str(a.get("query", "")), int(a.get("limit", 20)))
        return {
            "songs": [s.to_dict() for s in res.songs],
            "albums": [al.to_dict() for al in res.albums],
            "artists": [ar.to_dict() for ar in res.artists],
        }

    async def _remote_enqueue(self, a: dict) -> dict:
        """Queue a searched song by id (now or next). Looks it up via search
        so a client only needs the id it already saw in a `search` result."""
        song_id = str(a.get("song_id", ""))
        if self.client is None or not song_id:
            return {"queued": False}
        song = next((s for s in self._songs if s.id == song_id), None)
        if song is None:
            song = next((s for s in self.queue.songs if s.id == song_id), None)
        if song is None:  # not on screen: re-fetch via search
            res = await self.client.search(song_id, 20)
            song = next((s for s in res.songs if s.id == song_id), None)
        if song is None:
            return {"queued": False}
        if a.get("next"):
            self.queue.add_next([song])
        else:
            self.queue.add([song])
        self._render_queue()
        self._persist_queue()
        self._announce()
        return {"queued": True, "title": song.title}

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
        self.remote.publish(self._remote_snapshot())

    def _render_status(self) -> None:
        if self.client is None:
            return
        host = self.client.server.split("://", 1)[-1]
        text = Text()
        if self._offline:
            text.append(f"{icons.PLUG} offline  ", style=palette.yellow)
        if self.client.max_bitrate:
            # signal glyph + cap, shown only while streaming is capped
            text.append(f"\uf012 {self.client.max_bitrate}k  ", style=palette.yellow)
        text.append(f"{self.client.username}@{host}", style=palette.dim)
        self.query_one("#status", Static).update(text)

    # ── the heartbeat (all constant animation) ────────────────────────
    def _heartbeat(self) -> None:
        try:
            self.query_one("#logo", Logo).tick()
            now = self.query_one("#now", NowPlaying)
            level = None
            if self.player is not None:
                # jukebox has no mpv thread: poll server status for position on
                # this same heartbeat (no extra timer) so on_position still fires
                poll = getattr(self.player, "poll", None)
                if poll is not None:
                    poll()
                now.set_playing(self.player.active and not self.player.paused)
                now.set_class(self.player.active, "playing")
                level = self.player.level
            # sleep-timer countdown: reuse this one heartbeat, no new timer.
            # Show mm:ss remaining and fire (pause) when the deadline passes.
            if self._sleep_deadline is not None:
                remaining = self._sleep_deadline - time.monotonic()
                if remaining <= 0:
                    self._sleep_fire()
                else:
                    now.sleep_label = anim.fmt_time(remaining)
            now.tick(level)
            # drive the synced-lyrics highlight off this one heartbeat
            top = self.screen_stack[-1] if len(self.screen_stack) > 1 else None
            if isinstance(top, LyricsModal) and self.player is not None:
                top.tick(self.player.position)
            if self._zen:
                self._render_zen_info()  # follow track changes in the splash
            groups = {w.group for w in self.workers if not w.is_finished}
            panel = self.query_one("#tracks-panel")
            spin = anim.spinner(int(now._tick))
            if "download" in groups:
                panel.border_subtitle = f"{spin} downloading {self._dl_done}/{self._dl_total}"
            elif groups & {"lib", "songs"}:
                panel.border_subtitle = f"{spin} refreshing"
            elif panel.border_subtitle and (
                "refreshing" in panel.border_subtitle or "downloading" in panel.border_subtitle
            ):
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

        # podcasts: one row per subscribed channel → its episodes; the section
        # only appears once the server tells us there are any (no empty header)
        if self._podcasts:
            options.append(Option(Text(" "), disabled=True))
            options.append(Option(Text(" podcasts", style=f"bold {palette.dim}"), disabled=True))
            for channel, episodes in self._podcasts:
                row = Text(no_wrap=True, overflow="ellipsis")
                row.append(f"{PODCAST_GLYPH} ", style=palette.green)
                row.append(channel.title, style=palette.text)
                row.append(f" {len(episodes)}", style=palette.vfaint)
                options.append(Option(row, id=f"podcast:{channel.id}"))

        # internet radio: a single row loading every station into the pane
        if self._stations:
            options.append(Option(Text(" "), disabled=True))
            options.append(Option(Text(" internet radio", style=f"bold {palette.dim}"), disabled=True))
            row = Text(no_wrap=True, overflow="ellipsis")
            row.append(f"{RADIO_GLYPH} ", style=palette.blue)
            row.append("stations", style=palette.text)
            row.append(f" {len(self._stations)}", style=palette.vfaint)
            options.append(Option(row, id="radio"))

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
        if view_id.startswith("podcast:"):
            cid = view_id.split(":", 1)[1]
            channel = next((c for c, _ in self._podcasts if c.id == cid), None)
            return f"podcast · {channel.title}" if channel else "podcast"
        if view_id == "radio":
            return "internet radio"
        return VIEW_LABELS.get(view_id, "tracks")

    def _view_rows(self, view_id: str) -> list[Song]:
        """The playable rows for a podcast channel / the radio view, straight
        from the in-memory state loaded by `_load_podcasts_radio`."""
        if view_id == "radio":
            return list(self._stations)
        cid = view_id.split(":", 1)[1]
        return next((list(eps) for c, eps in self._podcasts if c.id == cid), [])

    def _show_songs(self, songs: list[Song], title: str) -> None:
        self._songs = songs
        # a fresh view supersedes any active filter / selection (the rows
        # changed under both)
        if self._filtering:
            self._exit_filter()
        self._clear_selection()
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
        elif view_id.startswith("podcast:") or view_id == "radio":
            # episodes / stations already live in memory (fetched + cached by
            # `_load_podcasts_radio`); just drop the rows into the pane
            self._show_songs(self._view_rows(view_id), title)
            return
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

    def _restore_podcasts_radio(self) -> None:
        """Rehydrate podcasts + stations from cache so their sidebar sections
        render instantly on launch (before the network worker runs)."""
        cached = self.dirs.read_cache("podcasts")
        if cached:
            self._podcasts = [
                (PodcastChannel.from_dict(c), [Song.from_dict(s) for s in eps])
                for c, eps in cached.get("channels", [])
            ]
        cached = self.dirs.read_cache("radio")
        if cached:
            self._stations = [Song.from_dict(s) for s in cached.get("stations", [])]

    @work(exclusive=True, group="lib")
    async def _load_podcasts_radio(self) -> None:
        """Refresh podcast channels+episodes and radio stations off the network.
        Each half degrades on its own: a server without podcasts (or radio)
        just leaves that section absent, never an error."""
        if self.client is None:
            return
        try:
            self._podcasts = await self.client.get_podcasts()
        except Exception:
            pass  # no podcasts / older server — keep whatever cache we had
        else:
            self.dirs.write_cache("podcasts", {
                "channels": [
                    [c.to_dict(), [s.to_dict() for s in eps]] for c, eps in self._podcasts
                ]
            })
        try:
            self._stations = await self.client.get_internet_radio_stations()
        except Exception:
            pass
        else:
            self.dirs.write_cache("radio", {"stations": [s.to_dict() for s in self._stations]})
        self._render_sidebar()
        # if the pane is showing a podcast/radio view, refresh its rows in place
        if self.view.startswith("podcast:") or self.view == "radio":
            self._show_songs(self._view_rows(self.view), self._tracks_title(self.view))

    # ── row rendering ─────────────────────────────────────────────────
    def _song_row(self, s: Song) -> Option:
        current = self.queue.current
        is_current = current is not None and s.id == current.id
        row = Text(no_wrap=True, overflow="ellipsis")
        if s.id in self._selected:
            # a selected row wins the marker column: a filled check
            row.append(f" {icons.CHECK_CIRCLE} ", style=palette.green)
        else:
            marker = anim.NOTE_FRAMES[0] if is_current else "·"
            row.append(f" {marker} ", style=palette.blue if is_current else palette.vfaint)
        row.append(s.title, style=f"bold {palette.blue}" if is_current else palette.text)
        if self.client is not None and self.client.cached_stream(s.id):
            row.append(f" {icons.CHECK}", style=palette.green)  # pinned for offline
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
    def _tracks_view(self) -> list[Song]:
        """The list the tracks pane is currently showing: the filtered subset
        while filter mode is active, otherwise the full `_songs`. Row indices
        and highlights map onto this, so play/enqueue/star stay correct."""
        return self._filtered if self._filtering else self._songs

    @on(OptionList.OptionHighlighted, "#tracks-list")
    def _track_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if self.player is not None and self.player.active:
            return  # while playing, the cover belongs to the current song
        song = next((s for s in self._songs if s.id == event.option.id), None)
        if song is not None and song.cover_art:
            self._load_art(song.cover_art, f"song-{song.id}")

    @on(OptionList.OptionSelected, "#tracks-list")
    def _track_selected(self, event: OptionList.OptionSelected) -> None:
        view = self._tracks_view()
        idx = next((i for i, s in enumerate(view) if s.id == event.option.id), None)
        if idx is not None:
            self._play_songs(view, idx)

    @on(OptionList.OptionSelected, "#queue-list")
    def _queue_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.highlighted is not None:
            self._change_track(lambda i=event.option_list.highlighted: self.queue.jump(i))

    # ── type-to-filter (an explicit mode over the tracks pane) ─────────
    # The app binds many bare single letters to global actions, so we never
    # let typing narrow the list "ambiently". `\` opens filter mode; while it
    # is active `on_key` (dispatched before App._on_key, which resolves the
    # bindings) eats printable keys into the query and prevents the default
    # binding, so `s`/`f`/`a`/… type instead of shuffling/starring/queueing.
    # j/k/g/G/up/down/enter fall through untouched, so list navigation and
    # "play the highlighted match" keep working; esc restores the full list.
    def action_filter(self) -> None:
        if not self._songs:
            self.notify("nothing here to filter", timeout=2)
            return
        self.query_one("#tracks-list", ClickList).focus()
        self._filtering = True
        self._filter_query = ""
        self._filtered = list(self._songs)
        self._render_filter_bar()

    # keys the count buffer treats as a list motion, mapped to the NavList
    # action that performs one step of it. j/k/down/up move by one row; g/G
    # jump to the ends (a count there is meaningless, so it just runs once).
    _COUNT_MOTIONS = {
        "j": "cursor_down", "down": "cursor_down",
        "k": "cursor_up", "up": "cursor_up",
        "g": "first", "G": "last",
    }
    _COUNT_LISTS = {"tracks-list", "queue-list", "sidebar-list"}

    def on_key(self, event) -> None:
        # select mode: intercept `space` (globally play/pause) into
        # toggle-current-and-advance, but ONLY while our mode is active and the
        # tracks pane is focused — so we never hijack the global bind elsewhere.
        # Written before the filter branch and guarded by its own flag so it
        # composes with type-to-filter and any other on_key consumer.
        if self._select_mode:
            focused = self.focused
            in_tracks = focused is not None and focused.id == "tracks-list"
            if event.key == "escape":
                self._exit_select_mode()
                event.prevent_default()
                event.stop()
                return
            if in_tracks and event.key == "space":
                self._toggle_current_selection()
                event.prevent_default()
                event.stop()
                return
            # j/k/g/G/arrows fall through so navigation still works
        # vim repeat counts: a digit pressed on a focused list arms a count for
        # the *next* keystroke. Digits 1-5 still rate the track immediately (the
        # binding fires as normal — we don't consume the event); we only stash
        # the pending count so an immediately-following motion repeats. This is
        # the clean no-timer resolution of the digit/rating tension: a bare
        # digit always rates, and `3j` moves three rows. Runs before the filter
        # branch and before the binding system, but only when NOT filtering and
        # a list is focused, so modals/search/transport are untouched.
        if not self._filtering:
            self._handle_count(event)
            return
        key = event.key
        if key == "escape":
            self._exit_filter()
            event.prevent_default()
            event.stop()
            return
        # navigation + selection belong to the (focused) tracks list; enter
        # plays the highlighted match. Let these reach the binding system.
        if key in ("up", "down", "j", "k", "g", "G", "home", "end",
                   "pageup", "pagedown", "enter", "tab", "shift+tab"):
            return
        if key == "backspace":
            self._filter_query = self._filter_query[:-1]
            self._apply_filter()
            event.prevent_default()
            event.stop()
            return
        if event.is_printable and event.character:
            self._filter_query += event.character
            self._apply_filter()
            event.prevent_default()
            event.stop()

    def _handle_count(self, event) -> None:
        """Apply / arm a vim repeat count for the focused list.

        A count armed by the previous keystroke lives for exactly one key:
        if this key is a motion, run it `count` times and consume the extra
        steps; otherwise the count is dropped. Digits 1-9 (and 0 only when a
        count is already building) re-arm the count and fall through so the
        `rate` binding still fires — so a bare digit rates and `3j` moves
        three rows. Only tracks/queue/sidebar; modals and search never reach
        here because their own focus owns the keys."""
        focused = self.focused
        if focused is None or focused.id not in self._COUNT_LISTS:
            self._count = ""
            return
        key = event.key
        pending, self._count = self._count, ""  # consume; re-arm below if digit

        action = self._COUNT_MOTIONS.get(key)
        if action is not None and pending:
            # let the first step run via the normal binding; drive the rest
            # here, then swallow nothing — the binding still performs step one
            try:
                repeat = max(1, int(pending)) - 1
            except ValueError:
                repeat = 0
            if action in ("first", "last"):
                repeat = 0  # jumping to an end more than once is a no-op
            for _ in range(min(repeat, 500)):  # cap: never loop unboundedly
                getattr(focused, f"action_{action}")()
            return

        if len(key) == 1 and key.isdigit():
            # arm the count for the next keystroke. Non-zero starts a count;
            # 0 only extends one. Fall through (no prevent_default) so digits
            # 1-5 still hit the rate binding.
            if key != "0" or pending:
                self._count = pending + key

    def _matches(self, song: Song, needle: str) -> bool:
        """Case-insensitive substring over title + artist, with a light
        subsequence fallback so 'dyln' still finds 'Bob Dylan'."""
        hay = f"{song.title} {song.artist}".lower()
        if needle in hay:
            return True
        it = iter(hay)
        return all(ch in it for ch in needle)

    def _apply_filter(self) -> None:
        needle = self._filter_query.lower().strip()
        self._filtered = (
            [s for s in self._songs if self._matches(s, needle)] if needle else list(self._songs)
        )
        self._fill("#tracks-list", [self._song_row(s) for s in self._filtered])
        self._render_filter_bar()

    def _render_filter_bar(self) -> None:
        panel = self.query_one("#tracks-panel")
        count = len(self._filtered)
        query = self._filter_query or " "
        panel.border_subtitle = f"{icons.FILTER} {query}  {count}/{len(self._songs)}"

    def _exit_filter(self) -> None:
        if not self._filtering:
            return
        self._filtering = False
        self._filter_query = ""
        self._filtered = []
        # restore the full list and let the heartbeat reset the subtitle
        panel = self.query_one("#tracks-panel")
        panel.border_subtitle = str(len(self._songs)) if self._songs else None
        self._fill("#tracks-list", [self._song_row(s) for s in self._songs])

    # ── multi-select (bulk actions over a set of song ids) ─────────────
    # `v` toggles select mode; in it, `space` toggles the highlighted row and
    # moves down (j/k still navigate). The selection is a set of ids, so it
    # survives re-renders (marker, star, download) and maps onto whichever
    # view — filtered or full — the tracks pane is showing. When the underlying
    # list changes (new view / refresh) the set is cleared via `_clear_selection`.
    def action_toggle_select_mode(self) -> None:
        if self._select_mode:
            self._exit_select_mode()
            return
        if not self._songs:
            self.notify("nothing here to select", timeout=2)
            return
        self.query_one("#tracks-list", ClickList).focus()
        self._select_mode = True
        self.notify("select mode — space toggles rows, esc exits", timeout=3)
        self._render_select_subtitle()

    def _exit_select_mode(self) -> None:
        if not self._select_mode:
            return
        self._select_mode = False
        self._selected.clear()
        self._refresh_song_markers()
        # let the heartbeat reset the subtitle (unless filter owns it)
        if not self._filtering:
            panel = self.query_one("#tracks-panel")
            panel.border_subtitle = str(len(self._songs)) if self._songs else None
        else:
            self._render_filter_bar()

    def _clear_selection(self) -> None:
        """Drop the selection when the rows change under it (view/refresh)."""
        self._select_mode = False
        self._selected.clear()

    def _toggle_current_selection(self) -> None:
        ol = self.query_one("#tracks-list", NavList)
        view = self._tracks_view()
        if ol.highlighted is None or ol.highlighted >= len(view):
            return
        song = view[ol.highlighted]
        if song.id in self._selected:
            self._selected.discard(song.id)
        else:
            self._selected.add(song.id)
        # re-render just this row's marker, then step down for fast tagging
        self._refresh_song_markers()
        if ol.highlighted < len(view) - 1:
            ol.highlighted += 1
        self._render_select_subtitle()

    def _render_select_subtitle(self) -> None:
        # don't fight the filter bar for the subtitle while filtering
        if self._filtering:
            self._render_filter_bar()
            return
        panel = self.query_one("#tracks-panel")
        n = len(self._selected)
        panel.border_subtitle = f"{icons.CHECK_CIRCLE} {n} selected"

    def _selected_songs(self) -> list[Song]:
        """The selected songs, in the order they appear in the current view."""
        if not self._selected:
            return []
        return [s for s in self._tracks_view() if s.id in self._selected]

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
        self._change_track(lambda: self.queue.set_songs(songs, start))

    def _stream_source(self, song: Song) -> str | None:
        """Where to play `song` from: a pinned local file if we have one,
        else the server stream URL. In offline mode a missing pin means the
        track is unplayable (None)."""
        # internet-radio stations carry a direct URL mpv opens as-is; they are
        # live streams, so never pinned and never available offline
        if song.stream_url:
            return None if self._offline else song.stream_url
        # jukebox plays on the SERVER, which already has the originals — always
        # hand it the stream URL (a local pin path means nothing to the server)
        if self._jukebox and self.client is not None:
            return self.client.stream_url(song.id)
        local = self.client.cached_stream(song.id) if self.client else None
        if local is not None:
            return str(local)
        if self._offline:
            return None
        return self.client.stream_url(song.id) if self.client else None

    def _play_current(self, resume_at: float = 0.0) -> None:
        song = self.queue.current
        now = self.query_one("#now", NowPlaying)
        if song is None:
            self.player.stop()
            now.set_song(None)
            self._tint_from_art(None)
            self._render_queue()
            return
        source = self._stream_source(song)
        if source is None:
            # offline and not pinned — skip forward to the next playable track,
            # scanning the queue at most once so an all-unpinned queue stops
            # rather than spinning
            for _ in range(len(self.queue.songs)):
                nxt = self.queue.advance(natural=False)
                if nxt is None:
                    break
                source = self._stream_source(nxt)
                if source is not None:
                    song = nxt
                    break
            if source is None:
                self.notify("offline: nothing downloaded to play", timeout=3)
                self.player.stop()
                now.set_song(None)
                now.set_playing(False)
                self._render_queue()
                return
        # jukebox status doesn't reliably report track length; feed it the
        # library duration so progress/seek math works (no-op for local mpv)
        set_duration = getattr(self.player, "set_duration", None)
        if set_duration is not None:
            set_duration(song.duration)
        self.player.play(source, start=resume_at)
        now.set_song(song)
        now.set_progress(resume_at, song.duration)
        self._scrobbled = False
        self._scrobble(song, False)
        if song.cover_art:
            self._load_art(song.cover_art, f"song-{song.id}")
        else:
            self._tint_from_art(None)
        self._render_queue()
        self._refresh_song_markers()
        self._persist_queue()
        self._announce(track_change=True)
        self._prefetch_next()

    def _prefetch_next(self) -> None:
        """Warm the next queued track's stream so its start is instant (and
        gapless is hardened). Peeks the queue without mutating it, dedups on the
        song id we last warmed, and skips anything already pinned or a case
        where prefetch can't help (offline mode, no client, no next track,
        repeat-one)."""
        if self.client is None or self._offline:
            return
        nxt = self.queue.peek_next()
        # peek_next returns `current` under repeat-one — nothing to warm there
        if nxt is None or (self.queue.current is not None and nxt.id == self.queue.current.id):
            return
        if nxt.id == self._prefetched or self.client.cached_stream(nxt.id):
            return
        self._prefetched = nxt.id
        self._warm_next(nxt)

    @work(exclusive=True, group="prefetch")
    async def _warm_next(self, song: Song) -> None:
        """Pin the next track to the audio cache off the UI thread. Reuses the
        offline-download path so a prefetched track is also available offline;
        stays silent (no notify) and swallows failures — it's pure speculation,
        and a miss just means the normal stream URL is used when it plays."""
        try:
            await self.client.download_song(song.id)
        except Exception:
            self._prefetched = None  # let a retry happen next time round
            return
        # the ✓ marker can now show for the freshly-pinned track
        if self.is_running:
            self._refresh_song_markers()

    def _refresh_song_markers(self) -> None:
        """Re-render the tracks pane so the ♪ marker (and stars, ✓, ratings)
        follow the player — over the filtered view when one is active."""
        self._fill("#tracks-list", [self._song_row(s) for s in self._tracks_view()])

    def action_play_pause(self) -> None:
        if self.player.active:
            self.player.toggle_pause()
            self._announce()
        elif self.queue.current is not None:
            # resume a restored queue exactly where it left off
            self._play_current(resume_at=self._resume_position)
            self._resume_position = 0.0

    def _change_track(self, pick) -> None:
        """Manual track change (skip/prev/jump): `pick()` moves the queue and
        returns the new song. With crossfade on and audio already playing, do a
        short fade-out first, then load + fade-in; otherwise switch instantly.
        Natural EOF doesn't come through here — gapless owns that seam."""
        if self._crossfade > 0 and self.player.active:
            # a rapid re-skip cancels the in-flight fade mid-ramp, so the live
            # volume may be lowered; capture the *true* user volume once (here,
            # synchronously) and keep it in _fade_base so successive skips don't
            # ratchet it down. Cleared only when a fade lands cleanly.
            if self._fade_base is None:
                self._fade_base = self.player.volume
            self._crossfade_change(pick, self._fade_base)
            return
        song = pick()
        if song is not None:
            self._play_current()

    @work(exclusive=True, group="crossfade")
    async def _crossfade_change(self, pick, base: int) -> None:
        half = self._crossfade / 2
        await self.player.fade_out(half)
        song = pick()
        if song is None:
            self.player.set_volume(base)  # nothing to play — undo the fade
            self._fade_base = None
            return
        self._play_current()  # loads + starts the next track at low volume
        await self.player.fade_in(base, half)
        self._fade_base = None  # landed cleanly; a cancel skips this line

    def action_next_track(self) -> None:
        self._change_track(lambda: self.queue.advance(natural=False))

    def action_prev_track(self) -> None:
        if self.player.position > 4:
            self.player.seek_to(0.0)
            return
        self._change_track(self.queue.prev)

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

    # ── playback speed ────────────────────────────────────────────────
    def action_cycle_speed(self) -> None:
        """Step through common speeds — handy for podcasts and audiobooks."""
        current = self.player.speed
        idx = min(
            range(len(SPEED_STEPS)),
            key=lambda i: abs(SPEED_STEPS[i] - current),
        )
        speed = self.player.set_speed(SPEED_STEPS[(idx + 1) % len(SPEED_STEPS)])
        now = self.query_one("#now", NowPlaying)
        now.speed = speed
        now.flash_speed()
        self.dirs.save_state({"speed": speed})
        self.notify(f"speed {speed:g}x", timeout=1.5)

    # ── sleep timer ───────────────────────────────────────────────────
    def action_cycle_sleep(self) -> None:
        """Cycle off → 15 → 30 → 45 → 60 min → end of track → off. The
        deadline is checked inside the heartbeat; there is no extra timer."""
        self._sleep_idx = (self._sleep_idx + 1) % len(SLEEP_PRESETS)
        preset = SLEEP_PRESETS[self._sleep_idx]
        now = self.query_one("#now", NowPlaying)
        if preset == 0:
            self._sleep_deadline = None
            now.sleep_label = ""
            self.notify("sleep timer off", timeout=1.5)
        elif preset == -1:
            self._sleep_deadline = None  # fired from _on_track_end instead
            now.sleep_label = "end"
            self.notify("sleep: stopping at end of track", timeout=2)
        else:
            self._sleep_deadline = time.monotonic() + preset * 60
            now.sleep_label = anim.fmt_time(preset * 60)
            self.notify(f"sleep timer: {preset} min", timeout=2)

    def _sleep_fire(self) -> None:
        """Pause playback and clear the timer once a deadline passes."""
        self._sleep_idx = 0
        self._sleep_deadline = None
        now = self.query_one("#now", NowPlaying)
        now.sleep_label = ""
        if self.player is not None and self.player.active and not self.player.paused:
            self.player.set_paused(True)
            now.set_playing(False)
            self._announce()
        self.notify("sleep timer — paused", timeout=5)

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

    # ── endless radio ─────────────────────────────────────────────────
    def action_toggle_radio(self) -> None:
        """Toggle autoplay-when-empty: keep pulling similar tracks forever."""
        self._radio = not self._radio
        self.dirs.save_state({"radio": self._radio})
        self.notify(
            "radio on — the queue refills itself" if self._radio else "radio off",
            timeout=2,
        )

    def action_start_radio(self) -> None:
        """Seed an endless station from the highlighted (or playing) track."""
        song = self._target_song()
        if song is None:
            self.notify("highlight a track to start radio", timeout=2)
            return
        if self.client is None:
            return
        self._radio = True
        self.dirs.save_state({"radio": True})
        self.query_one("#now", NowPlaying).repeat = self.queue.repeat
        self._play_songs([song], 0)  # start clean from the seed
        self.notify(f"radio: {song.title}", timeout=3)
        self._radio_refill(song, autoplay=False)  # prime the queue ahead

    @work(exclusive=True, group="radio")
    async def _radio_refill(self, seed: Song, autoplay: bool) -> None:
        """Fetch a bounded batch of songs like `seed` and append them. Runs
        off the UI thread. Falls back similar → top-songs → random, and stops
        quietly if all of those come up empty. `_radio_filling` guards against
        a runaway loop; ids already in the queue are dropped so we neither
        spam duplicates nor immediately re-queue what just played."""
        if self._radio_filling or self.client is None:
            return
        self._radio_filling = True
        try:
            batch: list[Song] = []
            try:
                batch = await self.client.get_similar_songs(seed.id, count=25)
                if not batch and seed.artist_id:
                    batch = await self.client.get_similar_songs(seed.artist_id, count=25)
                if not batch and seed.artist:
                    batch = await self.client.get_top_songs(seed.artist, count=25)
                if not batch:
                    batch = await self.client.get_random_songs(size=25)
            except Exception:
                batch = []
            have = {s.id for s in self.queue.songs}
            fresh = [s for s in batch if s.id not in have][:20]
            if not fresh:
                if autoplay:  # nothing to add and the queue is empty — stop calmly
                    self.player.stop()
                    now = self.query_one("#now", NowPlaying)
                    now.set_playing(False)
                    now.set_progress(0.0, 0.0)
                    self._render_queue()
                    self._announce()
                    self.notify("radio: no more tracks to play", timeout=3)
                return
            self.queue.add(fresh)
            if autoplay:
                song = self.queue.advance(natural=False)
                if song is not None:
                    self._play_current()
            else:
                self._render_queue()
                self._persist_queue()
        finally:
            self._radio_filling = False

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
                self._scrobble(song, True)
                # a play is now "counted" — mirror it into the local stats log
                # (cheap append; never blocks; matches the scrobble moment)
                self.stats.log_play(song.id, song.title, song.artist)
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
        # sleep timer set to "stop at end of track": let this one finish, then
        # stop rather than advancing, and clear the timer.
        if not failed and SLEEP_PRESETS[self._sleep_idx] == -1:
            self._sleep_idx = 0
            now = self.query_one("#now", NowPlaying)
            now.sleep_label = ""
            self.player.stop()
            now.set_playing(False)
            now.set_progress(0.0, 0.0)
            self._render_queue()
            self._announce()
            self.notify("sleep timer — stopped at end of track", timeout=5)
            return
        drained_seed = self.queue.current  # the track that just finished
        song = self.queue.advance(natural=not failed)
        if song is not None:
            self._play_current()
        elif self._radio and drained_seed is not None and self.client is not None:
            # queue ran dry with radio on: keep the music going by fetching a
            # batch of similar songs seeded from the track that just played
            self._radio_refill(drained_seed, autoplay=True)
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
        selected = self._selected_songs()
        if selected:  # bulk: queue the whole selection
            if play_next:
                self.queue.add_next(selected)
            else:
                self.queue.add(selected)
            self._render_queue()
            self._persist_queue()
            self.notify(
                f"queued {'next: ' if play_next else ''}{len(selected)} tracks", timeout=2
            )
            return
        ol = self.query_one("#tracks-list", NavList)
        view = self._tracks_view()
        if ol.highlighted is None or ol.highlighted >= len(view):
            return
        song = view[ol.highlighted]
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
            view = self._tracks_view()
            if ol.highlighted is not None and ol.highlighted < len(view):
                return view[ol.highlighted]
        elif focused is not None and focused.id == "queue-list":
            ol = self.query_one("#queue-list", NavList)
            if ol.highlighted is not None and ol.highlighted < len(self.queue.songs):
                return self.queue.songs[ol.highlighted]
        return None

    def action_playlist_add(self) -> None:
        # bulk when a selection exists and the tracks pane is focused, else the
        # single highlighted track — same PickerModal flow either way
        selected = (
            self._selected_songs()
            if self.focused is not None and self.focused.id == "tracks-list"
            else []
        )
        songs = selected or ([s] if (s := self._highlighted_song()) else [])
        if not songs:
            self.notify("highlight a track first (tracks or queue panel)", timeout=3)
            return
        label = songs[0].title if len(songs) == 1 else f"{len(songs)} tracks"
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
                    lambda name: self._playlist_create(name, songs) if name else None,
                )
            else:
                pid = choice.split(":", 1)[1]
                self._playlist_append(pid, songs)

        self.push_screen(PickerModal(f"add “{label}” to…", options), picked)

    def _playlist_created_name(self, name: str | None) -> None:
        if name:
            self._playlist_create(name, [])

    @work(group="mutate")
    async def _playlist_create(self, name: str, songs: list[Song]) -> None:
        self._mutations += 1
        try:
            await self.client.create_playlist(name, [s.id for s in songs])
        except Exception as e:
            self.notify(f"couldn't create playlist: {e}", severity="error", timeout=5)
            return
        finally:
            self._mutations -= 1
        if not songs:
            detail = ""
        elif len(songs) == 1:
            detail = f" with {songs[0].title}"
        else:
            detail = f" with {len(songs)} tracks"
        self.notify(f"created “{name}”" + detail, timeout=3)
        self._load_playlists()

    @work(group="mutate")
    async def _playlist_append(self, playlist_id: str, songs: list[Song]) -> None:
        self._mutations += 1
        try:
            await self.client.add_to_playlist(playlist_id, [s.id for s in songs])
        except Exception as e:
            self.notify(f"couldn't add to playlist: {e}", severity="error", timeout=5)
            return
        finally:
            self._mutations -= 1
        playlist = next((p for p in self._playlists if p.id == playlist_id), None)
        name = playlist.name if playlist else "playlist"
        detail = songs[0].title if len(songs) == 1 else f"{len(songs)} tracks"
        self.notify(f"added {detail} to “{name}”", timeout=3)
        self._invalidate_playlist(playlist_id)
        self._load_playlists()

    # ── editing the open playlist (pl:<id> view) ──────────────────────
    def _open_playlist_id(self) -> str | None:
        """The id of the playlist the tracks pane is showing, or None."""
        return self.view.split(":", 1)[1] if self.view.startswith("pl:") else None

    def _invalidate_playlist(self, playlist_id: str) -> None:
        """The playlist's cached songs are stale after any edit."""
        try:
            (self.dirs.cache_dir / f"playlist-songs-{playlist_id}.json").unlink()
        except OSError:
            pass

    def _playlist_track_index(self) -> int | None:
        """Row index of the highlighted track within the open playlist view.
        None unless a pl: view is open and the tracks list holds the cursor.
        Filter mode is disallowed — indices must map onto the server order."""
        if self._open_playlist_id() is None or self._filtering:
            return None
        focused = self.focused
        if focused is None or focused.id != "tracks-list":
            return None
        ol = self.query_one("#tracks-list", NavList)
        if ol.highlighted is None or ol.highlighted >= len(self._songs):
            return None
        return ol.highlighted

    def action_playlist_remove(self) -> None:
        """Remove the highlighted track from the currently-open playlist."""
        pid = self._open_playlist_id()
        idx = self._playlist_track_index()
        if pid is None or idx is None:
            self.notify("open a playlist and highlight a track to remove", timeout=3)
            return
        song = self._songs[idx]
        # optimistic: drop it from the model and re-render, then persist
        del self._songs[idx]
        title = self._tracks_title(self.view)
        self._show_songs(list(self._songs), title)
        ol = self.query_one("#tracks-list", NavList)
        if self._songs:
            ol.highlighted = min(idx, len(self._songs) - 1)
        self._playlist_remove_at(pid, idx, song.title)

    @work(group="mutate")
    async def _playlist_remove_at(self, playlist_id: str, index: int, title: str) -> None:
        self._mutations += 1
        try:
            await self.client.remove_from_playlist(playlist_id, [index])
        except Exception as e:
            self.notify(f"couldn't remove track: {e}", severity="error", timeout=5)
            self._invalidate_playlist(playlist_id)
            if self.view == f"pl:{playlist_id}":
                self._load_view(self.view)  # resync from the server
            return
        finally:
            self._mutations -= 1
        self.notify(f"removed “{title}”", timeout=2)
        self._invalidate_playlist(playlist_id)
        self._load_playlists()

    def action_playlist_move(self, delta: int) -> None:
        """Move the highlighted track up/down within the open playlist and
        persist the new order to the server."""
        pid = self._open_playlist_id()
        idx = self._playlist_track_index()
        if pid is None or idx is None:
            return
        new = idx + delta
        if new < 0 or new >= len(self._songs):
            return
        # optimistic reorder in the model, then persist the full order
        self._songs[idx], self._songs[new] = self._songs[new], self._songs[idx]
        title = self._tracks_title(self.view)
        self._show_songs(list(self._songs), title)
        self.query_one("#tracks-list", NavList).highlighted = new
        self._playlist_reorder(pid, [s.id for s in self._songs])

    @work(group="mutate")
    async def _playlist_reorder(self, playlist_id: str, song_ids: list[str]) -> None:
        self._mutations += 1
        try:
            await self.client.reorder_playlist(playlist_id, song_ids)
        except Exception as e:
            self.notify(f"couldn't reorder playlist: {e}", severity="error", timeout=5)
            self._invalidate_playlist(playlist_id)
            if self.view == f"pl:{playlist_id}":
                self._load_view(self.view)  # resync from the server
            return
        finally:
            self._mutations -= 1
        self._invalidate_playlist(playlist_id)

    def action_playlist_rename(self) -> None:
        pid = self._open_playlist_id()
        if pid is None:
            self.notify("open a playlist to rename it", timeout=3)
            return
        playlist = next((p for p in self._playlists if p.id == pid), None)
        current = playlist.name if playlist else ""
        self.push_screen(
            InputModal("rename playlist", placeholder=current),
            lambda name: self._playlist_rename(pid, name) if name else None,
        )

    @work(group="mutate")
    async def _playlist_rename(self, playlist_id: str, name: str) -> None:
        self._mutations += 1
        try:
            await self.client.rename_playlist(playlist_id, name)
        except Exception as e:
            self.notify(f"couldn't rename playlist: {e}", severity="error", timeout=5)
            return
        finally:
            self._mutations -= 1
        self.notify(f"renamed to “{name}”", timeout=3)
        if self.view == f"pl:{playlist_id}":
            self.query_one("#tracks-panel").border_title = name
        self._load_playlists()

    def action_playlist_delete(self) -> None:
        pid = self._open_playlist_id()
        if pid is None:
            self.notify("open a playlist to delete it", timeout=3)
            return
        playlist = next((p for p in self._playlists if p.id == pid), None)
        name = playlist.name if playlist else "this playlist"
        options = [
            Option(Text(f" {icons.CROSS_CIRCLE} delete “{name}”", style=palette.red), id="yes"),
            Option(Text(f" {icons.CHECK_CIRCLE} keep it", style=palette.sub), id="no"),
        ]

        def confirmed(choice: str | None) -> None:
            if choice == "yes":
                self._playlist_delete(pid, name)

        self.push_screen(PickerModal(f"delete “{name}”?", options), confirmed)

    @work(group="mutate")
    async def _playlist_delete(self, playlist_id: str, name: str) -> None:
        self._mutations += 1
        try:
            await self.client.delete_playlist(playlist_id)
        except Exception as e:
            self.notify(f"couldn't delete playlist: {e}", severity="error", timeout=5)
            return
        finally:
            self._mutations -= 1
        self.notify(f"deleted “{name}”", timeout=3)
        self._invalidate_playlist(playlist_id)
        # fall back to the default view if we were looking at the deleted one
        if self.view == f"pl:{playlist_id}":
            self.view = "all-songs"
            self.dirs.save_state({"view": self.view})
            self._highlight_view(self.view)
            self._load_view(self.view)
        self._load_playlists()

    def action_queue_save(self) -> None:
        """Save the current play queue as a brand-new playlist."""
        if not self.queue.songs:
            self.notify("the queue is empty — nothing to save", timeout=2)
            return
        song_ids = [s.id for s in self.queue.songs]
        self.push_screen(
            InputModal("save queue as playlist", placeholder="name"),
            lambda name: self._queue_save(name, song_ids) if name else None,
        )

    @work(group="mutate")
    async def _queue_save(self, name: str, song_ids: list[str]) -> None:
        self._mutations += 1
        try:
            await self.client.create_playlist(name, song_ids)
        except Exception as e:
            self.notify(f"couldn't save queue: {e}", severity="error", timeout=5)
            return
        finally:
            self._mutations -= 1
        self.notify(f"saved “{name}” · {len(song_ids)} tracks", timeout=3)
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
        if self._offline:
            self.mutations.rate(song_id, rating)
            self._note_queued()
            return
        self._mutations += 1
        try:
            await self.client.set_rating(song_id, rating)
        except Exception as e:
            if self._is_network_error(e):
                self.mutations.rate(song_id, rating)
                self._note_queued()
            else:
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
        # prefer timed (synced) lyrics; fall back to the plain getLyrics path
        synced = None
        get_synced = getattr(self.client, "get_synced_lyrics", None)
        if get_synced is not None:
            try:
                synced = await get_synced(song.id)
            except Exception:
                synced = None
        text = ""
        if not synced:
            try:
                text = await self.client.get_lyrics(song.artist, song.title)
            except Exception:
                text = ""
        if not synced and not text.strip():
            self.notify(f"no lyrics found for {song.title}", timeout=3)
            return
        self.push_screen(
            LyricsModal(f"{song.title} — {song.artist}", text, synced=synced)
        )

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

    def action_export_card(self) -> None:
        """Save the now-playing state as a shareable themed SVG card."""
        song = self.queue.current
        if song is None or self.player is None or not self.player.active:
            self.notify("nothing playing to export", timeout=2)
            return
        path = card.export_path(song, self.dirs.cache_dir)
        try:
            card.export_svg(song, self.player.position, self.player.duration, path)
        except Exception as e:
            self.notify(f"couldn't export card: {e}", severity="warning", timeout=5)
            return
        self.notify(f"saved card · {path}", timeout=5)

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

    # ── genre browse ──────────────────────────────────────────────────
    def action_pick_genre(self) -> None:
        """Pick a genre to load its songs into the tracks pane (ad-hoc view)."""
        if self.client is None:
            return
        cached = self.dirs.read_cache("genres")
        genres = [Genre.from_dict(g) for g in cached.get("genres", [])] if cached else []
        if genres:
            self._show_genre_picker(genres)
        self._fetch_genres(show=not genres)

    @work(exclusive=True, group="genres")
    async def _fetch_genres(self, show: bool) -> None:
        try:
            genres = await self.client.get_genres()
        except Exception as e:
            if show:
                self._connection_trouble(e)
            return
        self.dirs.write_cache("genres", {"genres": [g.to_dict() for g in genres]})
        if show:
            self._show_genre_picker(genres)

    def _show_genre_picker(self, genres: list[Genre]) -> None:
        if not genres:
            self.notify("no genres found", timeout=2)
            return
        options = [
            Option(
                Text.assemble(
                    (f" {icons.TAG} ", palette.mauve),
                    (g.name, palette.text),
                    (f"  {g.song_count}♪", palette.vfaint),
                ),
                id=g.name,
            )
            for g in genres
        ]
        self.push_screen(PickerModal("browse by genre", options), self._genre_picked)

    def _genre_picked(self, name: str | None) -> None:
        if name:
            self._load_genre_songs(name)

    @work(exclusive=True, group="songs")
    async def _load_genre_songs(self, genre: str) -> None:
        """Ad-hoc view: every song tagged with `genre`. Cache-first, mirrors
        the artist/album ad-hoc loaders."""
        title = f"genre · {genre}"
        self.view = f"genre:{genre}"
        self._highlight_view(None)
        cache_key = f"genre-{genre}"
        cached = self.dirs.read_cache(cache_key)
        if cached:
            self._show_songs([Song.from_dict(s) for s in cached.get("songs", [])], title)
        try:
            songs = await self.client.get_songs_by_genre(genre)
        except Exception as e:
            self._connection_trouble(e)
            return
        self.dirs.write_cache(cache_key, {"songs": [s.to_dict() for s in songs]})
        if self.view == f"genre:{genre}":
            self._show_songs(songs, title)
            self.query_one("#tracks-list", ClickList).focus()

    # ── bookmarks (resume long tracks / audiobooks) ───────────────────
    def action_bookmark(self) -> None:
        """Save a resume point at the CURRENT playback position."""
        song = self.queue.current
        if song is None or self.player is None or not self.player.active:
            self.notify("nothing playing to bookmark", timeout=2)
            return
        position_ms = int(max(0.0, self.player.position) * 1000)
        self._create_bookmark(song, position_ms)

    @work(group="mutate")
    async def _create_bookmark(self, song: Song, position_ms: int) -> None:
        try:
            await self.client.create_bookmark(song.id, position_ms)
        except Exception as e:
            self.notify(f"couldn't bookmark: {e}", severity="warning", timeout=5)
            return
        try:
            self.dirs.cache_dir.joinpath("bookmarks.json").unlink()
        except OSError:
            pass
        self.notify(
            f"bookmarked {song.title} at {anim.fmt_time(position_ms // 1000)}", timeout=3
        )

    def action_pick_bookmark(self) -> None:
        """Pick a saved bookmark to resume: plays the song and seeks to it."""
        if self.client is None:
            return
        cached = self.dirs.read_cache("bookmarks")
        marks = [Bookmark.from_dict(b) for b in cached.get("bookmarks", [])] if cached else []
        if marks:
            self._show_bookmark_picker(marks)
        self._fetch_bookmarks(show=not marks)

    @work(exclusive=True, group="bookmarks")
    async def _fetch_bookmarks(self, show: bool) -> None:
        try:
            marks = await self.client.get_bookmarks()
        except Exception as e:
            if show:
                self._connection_trouble(e)
            return
        self.dirs.write_cache("bookmarks", {"bookmarks": [b.to_dict() for b in marks]})
        if show:
            self._show_bookmark_picker(marks)

    def _show_bookmark_picker(self, marks: list[Bookmark]) -> None:
        if not marks:
            self.notify("no bookmarks yet — press w while playing", timeout=3)
            return
        options = []
        for i, m in enumerate(marks):
            row = Text.assemble(
                (f" {BOOKMARK_GLYPH} ", palette.peach),
                (m.song.title, palette.text),
                (f"  {m.song.artist}", palette.dim),
                (f"  @ {anim.fmt_time(m.position_ms // 1000)}", palette.vfaint),
            )
            options.append(Option(row, id=str(i)))
        self._pending_bookmarks = marks
        self.push_screen(PickerModal("resume a bookmark", options), self._bookmark_picked)

    def _bookmark_picked(self, index: str | None) -> None:
        if index is None:
            return
        marks = getattr(self, "_pending_bookmarks", [])
        try:
            mark = marks[int(index)]
        except (ValueError, IndexError):
            return
        # play the bookmarked song solo, seeking straight to the saved point
        self.queue.set_songs([mark.song], 0)
        self._play_current(resume_at=mark.position_ms / 1000.0)

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

    # ── offline mutation queue ────────────────────────────────────────
    @staticmethod
    def _is_network_error(error: Exception) -> bool:
        """A connectivity failure (park the mutation) vs. a server rejection
        (a bad id — surface it, don't retry forever). Mirrors the split in
        `_connection_trouble`: SubsonicError is the server saying no."""
        return not isinstance(error, SubsonicError)

    def _note_queued(self) -> None:
        """Unobtrusive breadcrumb that a write is parked for later."""
        n = self.mutations.pending
        self.notify(f"offline — {n} change{'s' if n != 1 else ''} queued", timeout=2)

    @work(group="flush")
    async def _flush_mutations(self) -> None:
        """Replay parked stars/ratings/scrobbles once we look online again.
        Runs off the UI thread; drops each op on success, keeps the rest on the
        first network failure so order and intent are preserved."""
        if self.client is None or self._offline or not self.mutations.pending:
            return
        try:
            flushed = await self.mutations.flush(self.client, self._is_network_error)
        except Exception:
            return
        if flushed:
            self.notify(f"synced {flushed} queued change{'s' if flushed != 1 else ''}", timeout=2)

    # ── starring ──────────────────────────────────────────────────────
    def action_star(self) -> None:
        selected = self._selected_songs()
        if selected and self.focused is not None and self.focused.id == "tracks-list":
            # bulk: star every selected track (idempotent — always set on, so a
            # mixed selection ends up uniformly starred)
            for song in selected:
                if not song.starred:
                    song.starred = True
                    self._star(song.id, "song", True)
            self._refresh_song_markers()
            self._render_queue()
            current = self.queue.current
            if current is not None and current.id in self._selected:
                current.starred = True
                self.query_one("#now", NowPlaying).song = current
            self.notify(f"starred {len(selected)} tracks", timeout=2)
            return
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
        # offline mode: never touch the network — the cache already reflects it
        if self._offline:
            self.mutations.star(item_id, kind, star)
            self._note_queued()
            return
        self._mutations += 1
        try:
            await self.client.set_star(item_id, kind, star)
        except Exception as e:
            if self._is_network_error(e):
                self.mutations.star(item_id, kind, star)
                self._note_queued()
            else:
                self.notify(f"couldn't {'star' if star else 'unstar'}: {e}", severity="warning")
        finally:
            self._mutations -= 1

    # ── offline downloads ─────────────────────────────────────────────
    def action_download(self) -> None:
        """Pin the selection (if any) or the highlighted/playing track."""
        selected = (
            self._selected_songs()
            if self.focused is not None and self.focused.id == "tracks-list"
            else []
        )
        if selected:
            self._download_songs(selected, label=f"{len(selected)} tracks")
            return
        song = self._target_song()
        if song is None:
            self.notify("highlight a track to download", timeout=2)
            return
        if song.stream_url:  # live radio stream — nothing to pin
            self.notify("internet radio can't be downloaded", timeout=2)
            return
        if self.client is not None and self.client.cached_stream(song.id):
            self.notify(f"already downloaded: {song.title}", timeout=2)
            return
        self._download_songs([song], label=song.title)

    def action_download_view(self) -> None:
        """Pin every track in the current tracks pane / playlist."""
        if not self._songs:
            self.notify("nothing here to download", timeout=2)
            return
        title = self.query_one("#tracks-panel").border_title or "view"
        self._download_songs(list(self._songs), label=str(title))

    def action_download_all(self) -> None:
        """Pin the whole loaded library (the all-tracks cache)."""
        cached = self.dirs.read_cache("all-songs")
        songs = [Song.from_dict(s) for s in cached.get("songs", [])] if cached else list(self._songs)
        if not songs:
            self.notify("library not loaded yet — open 'all tracks' first", timeout=3)
            return
        self._download_songs(songs, label="library")

    @work(exclusive=True, group="download")
    async def _download_songs(self, songs: list[Song], label: str) -> None:
        """Download a batch of songs to the audio cache. Runs off the UI
        thread; progress rides the heartbeat spinner (group='download'),
        completion/failure is a notify. Already-pinned songs are skipped
        cheaply so re-runs are near-instant."""
        if self.client is None:
            return
        # skip live radio streams (stream_url set) — only real files pin
        pending = [s for s in songs if not s.stream_url and not self.client.cached_stream(s.id)]
        if not pending:
            self.notify(f"{label}: already downloaded", timeout=2)
            return
        self._dl_total = len(pending)
        self._dl_done = 0
        self._dl_failed = 0
        if len(pending) > 1:
            self.notify(f"downloading {label} · {len(pending)} tracks", timeout=3)
        for song in pending:
            try:
                await self.client.download_song(song.id)
            except Exception:
                self._dl_failed += 1
            self._dl_done += 1
            # re-render so the ✓ appears as each track lands
            self._refresh_song_markers()
        ok = self._dl_done - self._dl_failed
        if self._dl_failed:
            self.notify(
                f"downloaded {ok}/{self._dl_total} · {self._dl_failed} failed",
                severity="warning", timeout=5,
            )
        else:
            self.notify(f"downloaded {label}" if ok == 1 else f"downloaded {ok} tracks", timeout=3)
        self._refresh_song_markers()

    def action_toggle_offline(self) -> None:
        self._offline = not self._offline
        self.dirs.save_state({"offline": self._offline})
        self._render_status()
        self.notify(
            "offline mode — playing only downloaded tracks" if self._offline
            else "online mode",
            timeout=3,
        )
        if not self._offline:
            self._flush_mutations()  # back online: replay what we buffered

    def action_cycle_quality(self) -> None:
        """Step through the streaming-quality presets. Updates the cap used for
        the next stream; a currently-playing track is left untouched."""
        if self.client is None:
            return
        current = (int(self.client.max_bitrate), str(self.client.stream_format))
        idx = next(
            (i for i, (_, kb, fmt) in enumerate(QUALITY_PRESETS) if (kb, fmt) == current),
            -1,
        )
        label, kbps, fmt = QUALITY_PRESETS[(idx + 1) % len(QUALITY_PRESETS)]
        self.client.max_bitrate = kbps
        self.client.stream_format = fmt
        self._render_status()
        self.notify(f"\uf012 stream quality: {label}", timeout=2)

    @work(group="mutate")
    async def _scrobble(self, song: Song, submission: bool) -> None:
        # best-effort but still worth buffering so play counts catch up; stays
        # silent (background write, no user gesture to acknowledge)
        if self._offline:
            self.mutations.scrobble(song.id, submission)
            # ListenBrainz needs full track metadata the offline mutation queue
            # (keyed by id) can't reconstruct, so its listens don't buffer — a
            # missed listen is harmless, and never touching the network here
            # keeps offline mode truly offline.
            return
        try:
            await self.client.scrobble(song.id, submission)
        except Exception as e:
            if self._is_network_error(e):
                self.mutations.scrobble(song.id, submission)
        # mirror the scrobble to ListenBrainz when configured (no-op otherwise):
        # "playing_now" on track start, a counted listen at the submit threshold
        if self.listenbrainz.enabled:
            try:
                if submission:
                    await self.listenbrainz.submit(song)
                else:
                    await self.listenbrainz.now_playing(song)
            except Exception:
                pass  # opt-in extra; never let it disturb playback

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
        self._load_podcasts_radio()  # re-pull feeds + stations (refreshes pane)
        if not self.view.startswith(("artist:", "album:", "genre:", "podcast:")) and self.view != "radio":
            self._load_view(self.view)  # ad-hoc views have no sidebar entry
        self.notify("refreshing", timeout=1.5)

    def _maybe_auto_refresh(self) -> None:
        if self.client is None or self._mutations > 0:
            return
        if self.screen is not self.screen_stack[0]:
            return  # modal open — don't yank state around underneath it
        self._load_playlists()
        self._load_podcasts_radio()
        if not self.view.startswith(("artist:", "album:", "genre:", "podcast:")) and self.view != "radio":
            self._load_view(self.view)
        # if we're reachable enough to auto-refresh, try draining the queue too;
        # the flush worker no-ops when offline or empty and re-parks on failure
        self._flush_mutations()

    def action_help(self) -> None:
        self.push_screen(HelpModal(HELP_SECTIONS, title="NaviTui · keys"))

    def action_stats(self) -> None:
        """Open the local listening-stats modal — reads the play log on open
        (cheap) and folds it into a summary; never touches the network."""
        import time

        summary = statsmod.summarize(self.stats.load(), time.time())
        self.push_screen(StatsModal(summary))

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
        self.notifier.stop()
        self.remote.stop()
        self.discord.stop()
        await self.listenbrainz.close()
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
