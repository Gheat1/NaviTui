"""Command palette — every meaningful action, searchable by name.

Textual ships a fuzzy command palette (ctrl+p); the app binds so many bare
single letters that memorising them all is a chore, so we surface the whole
verb list here. Each entry runs an existing ``action_*`` (via ``run_action``,
so args are the same Python-literal strings the bindings use). Registered
alongside — never instead of — Textual's built-in system commands.

Labels stay lowercase to match the app's aesthetic; icons are ``\\uXXXX``
escapes only (nerd-font FontAwesome, mirroring ricekit.icons). The command
list is a module constant but the hits are built per-search, so the palette
always reflects the app's real verbs.
"""

from __future__ import annotations

from textual.command import DiscoveryHit, Hit, Hits, Provider

# nerd-font glyphs as \uXXXX escapes only — raw PUA glyphs do not survive the
# patch tooling (see CLAUDE.md). These mirror ricekit.icons' FontAwesome set.
_PLAY = "\uf04b"      # nf-fa-play
_NEXT = "\uf051"      # nf-fa-step_forward
_PREV = "\uf048"      # nf-fa-step_backward
_SEARCH = "\uf002"    # nf-fa-search
_SHUFFLE = "\uf074"   # nf-fa-random
_REPEAT = "\uf01e"    # nf-fa-repeat
_FILTER = "\uf0b0"    # nf-fa-filter
_PLUS = "\uf067"      # nf-fa-plus
_MINUS = "\uf068"     # nf-fa-minus
_TRASH = "\uf1f8"     # nf-fa-trash
_RADIO = "\uf519"     # nf-fa-broadcast_tower
_STAR = "\uf005"      # nf-fa-star
_LIST = "\uf03a"      # nf-fa-list
_MUSIC = "\uf001"     # nf-fa-music
_LINK = "\uf0c1"      # nf-fa-link
_CAMERA = "\uf030"    # nf-fa-camera
_DOWNLOAD = "\uf019"  # nf-fa-download
_PLUG = "\uf1e6"      # nf-fa-plug
_BELL = "\uf0f3"      # nf-fa-bell
_ALBUM = "\uf51f"     # nf-fa-compact_disc
_MIC = "\uf130"       # nf-fa-microphone
_REFRESH = "\uf021"   # nf-fa-refresh
_PALETTE = "\uf1fc"   # nf-fa-paint_brush
_MOON = "\uf186"      # nf-fa-moon_o
_HELP = "\uf059"      # nf-fa-question_circle
_POWER = "\uf011"     # nf-fa-power_off

# (label, action, help). ``action`` is passed to App.run_action verbatim, so
# args are Python literals exactly as in the BINDINGS table (e.g. seek(30)).
COMMANDS: list[tuple[str, str, str]] = [
    (f"{_PLAY} play / pause", "play_pause", "toggle playback"),
    (f"{_NEXT} next track", "next_track", "skip to the next track"),
    (f"{_PREV} previous track", "prev_track", "back / restart the track"),
    (f"{_SEARCH} search", "search", "search songs, albums, artists"),
    (f"{_SHUFFLE} shuffle", "toggle_shuffle", "toggle shuffle"),
    (f"{_REPEAT} repeat", "cycle_repeat", "cycle repeat: off -> all -> one"),
    (f"{_FILTER} filter tracks", "filter", "narrow the tracks pane as you type"),
    (f"{_PLUS} queue track", "enqueue(False)", "add the highlighted track to the queue"),
    (f"{_PLUS} play next", "enqueue(True)", "queue the highlighted track next"),
    (f"{_MINUS} remove from queue", "queue_remove", "drop the highlighted queue track"),
    (f"{_TRASH} clear queue", "queue_clear", "empty the play queue"),
    (f"{_RADIO} start radio", "start_radio", "seed an endless station from this track"),
    (f"{_RADIO} toggle radio", "toggle_radio", "autoplay similar tracks when the queue drains"),
    (f"{_STAR} star / unstar", "star", "star or unstar the highlighted track"),
    (f"{_LIST} add to playlist", "playlist_add", "add the highlighted track to a playlist"),
    (f"{_MUSIC} lyrics", "lyrics", "show lyrics for the current track"),
    (f"{_LINK} copy share link", "share", "create and copy a share link"),
    (f"{_CAMERA} export card", "export_card", "save the now-playing state as an SVG card"),
    (f"{_DOWNLOAD} download track", "download", "pin the highlighted track for offline"),
    (f"{_DOWNLOAD} download view", "download_view", "pin every track in this pane for offline"),
    (f"{_DOWNLOAD} download library", "download_all", "pin the whole loaded library for offline"),
    (f"{_PLUG} offline mode", "toggle_offline", "play only downloaded tracks"),
    (f"{_BELL} notifications", "toggle_notifications", "toggle desktop notifications"),
    (f"{_ALBUM} go to album", "go_album", "open the track's album"),
    (f"{_MIC} go to artist", "go_artist", "open the track's artist"),
    (f"{_REFRESH} refresh", "refresh", "reload playlists and the current view"),
    (f"{_PALETTE} cycle theme", "cycle_kit_theme", "step to the next kit theme"),
    (f"{_PALETTE} pick theme", "change_theme", "open the theme picker with live preview"),
    (f"{_MOON} zen splash", "toggle_zen", "big now-playing splash"),
    (f"{_HELP} help", "help", "show the keybind cheatsheet"),
    (f"{_POWER} quit", "quit", "quit NaviTui"),
]


class NaviTuiCommands(Provider):
    """Expose every NaviTui action in the fuzzy command palette."""

    async def discover(self) -> Hits:
        for label, action, help_text in COMMANDS:
            yield DiscoveryHit(label, self._runner(action), help=help_text)

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for label, action, help_text in COMMANDS:
            if (score := matcher.match(label)) > 0:
                yield Hit(
                    score,
                    matcher.highlight(label),
                    self._runner(action),
                    help=help_text,
                )

    def _runner(self, action: str):
        # run_action parses the same "name(args)" strings the bindings use, so
        # literal args (enqueue(True), seek(30)) stay identical to the keymap
        app = self.app
        return lambda: app.run_action(action)
