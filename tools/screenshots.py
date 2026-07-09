"""README screenshot generator — fake library, generated cover art, a real
local audio file for playback. Zero network, deterministic output.

    .venv/bin/python tools/screenshots.py

Writes SVGs + PNGs to assets/ (PNGs need rsvg-convert on PATH).
"""

from __future__ import annotations

import asyncio
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# isolate all state/cache/config from the real user
FAKE_HOME = Path(tempfile.mkdtemp(prefix="navitui-shots-"))
os.environ["HOME"] = str(FAKE_HOME)
os.environ["NAVITUI_ART"] = "halfcell"

from PIL import Image, ImageDraw  # noqa: E402

from navitui.models import (  # noqa: E402
    Album,
    Artist,
    PodcastChannel,
    Playlist,
    SearchResults,
    Song,
)

ASSETS = REPO / "assets"

BANDS = [
    ("Neon Meridian", ["Parallax Drive", "Glass Highways"]),
    ("The Cassette Ghosts", ["Rewind Culture"]),
    ("Vantablack Orchard", ["Fruit of the Void", "Orchard at Night"]),
    ("Polyrhythm Committee", ["Quorum"]),
    ("Saturn Parking Lot", ["Meter Running"]),
    ("Moss Piglet", ["Tardigrade Summer"]),
    ("Analog Weather", ["Forecast: Reverb"]),
    ("The Umlaut Häus", ["Diacritical Hits"]),
]

TRACKS = [
    "First Light", "Static Bloom", "Half-Life Heart", "Chrome Lullaby",
    "Departure Gate C7", "Slow Voltage", "Peripheral Vision", "Night Bus Home",
    "Copper Wire Waltz", "The Long Now", "Signal Fade", "Afterimage",
]

PALETTES = [
    ((137, 180, 250), (203, 166, 247)),
    ((250, 179, 135), (243, 139, 168)),
    ((166, 227, 161), (137, 220, 235)),
    ((249, 226, 175), (235, 160, 172)),
    ((180, 190, 254), (148, 226, 213)),
    ((243, 139, 168), (137, 180, 250)),
    ((148, 226, 213), (249, 226, 175)),
    ((203, 166, 247), (166, 227, 161)),
]


def make_cover(index: int, path: Path) -> None:
    """A generative cover per album: smooth diagonal gradient, a soft glow,
    and one big translucent ring in an accent color. Deliberately
    low-frequency — fine detail just aliases into noise in a terminal."""
    (c1, c2) = PALETTES[index % len(PALETTES)]
    accent = PALETTES[(index + 3) % len(PALETTES)][0]
    size = 800
    img = Image.new("RGB", (size, size))
    px = img.load()
    cx, cy = size * 0.64, size * 0.36
    max_d = size * 0.6
    for y in range(size):
        for x in range(0, size, 4):  # 4px columns keep this fast
            t = (x + y) / (2 * size)
            base = [a + (b - a) * t for a, b in zip(c1, c2)]
            d = math.hypot(x - cx, y - cy) / max_d
            glow = max(0.0, 1.0 - d) ** 2 * 0.5
            col = tuple(round(v + (252 - v) * glow) for v in base)
            for dx in range(4):
                if x + dx < size:
                    px[x + dx, y] = col
    ring = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    rdraw = ImageDraw.Draw(ring)
    rx, ry, rr = size * 0.34, size * 0.66, size * 0.30
    rdraw.ellipse(
        (rx - rr, ry - rr, rx + rr, ry + rr),
        outline=(*accent, 110),
        width=round(size * 0.035),
    )
    img = Image.alpha_composite(img.convert("RGBA"), ring).convert("RGB")
    img.save(path, "PNG")


def make_tone(path: Path) -> None:
    """10s of a soft chord so mpv genuinely plays during the shot."""
    rate = 44100
    with wave.open(str(path), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(rate * 10):
            t = i / rate
            sample = 0.15 * (
                math.sin(2 * math.pi * 220 * t)
                + 0.6 * math.sin(2 * math.pi * 277.18 * t)
                + 0.4 * math.sin(2 * math.pi * 329.63 * t)
            )
            frames += struct.pack("<h", int(sample * 32767))
        w.writeframes(frames)


class FakeClient:
    """The same surface as SubsonicClient, no network anywhere."""

    server = "https://demo.music.example"
    username = "gheat"
    max_bitrate = 0
    stream_format = ""

    def __init__(self) -> None:
        self._art_dir = FAKE_HOME / "art"
        self._art_dir.mkdir(parents=True, exist_ok=True)
        self._tone = FAKE_HOME / "tone.wav"
        make_tone(self._tone)

        self.artists: list[Artist] = []
        self.albums: dict[str, list[Album]] = {}
        self.songs: dict[str, list[Song]] = {}
        album_n = 0
        for ai, (band, album_names) in enumerate(BANDS):
            artist = Artist(id=f"ar{ai}", name=band, album_count=len(album_names), starred=ai in (0, 2))
            self.artists.append(artist)
            self.albums[artist.id] = []
            for an, album_name in enumerate(album_names):
                album_id = f"al{album_n}"
                cover = f"cov{album_n}"
                make_cover(album_n, self._art_dir / f"{cover}-600")
                album = Album(
                    id=album_id, name=album_name, artist=band, artist_id=artist.id,
                    year=2018 + (album_n * 3) % 8, song_count=6, duration=6 * 222,
                    cover_art=cover, starred=(album_n % 3 == 0),
                )
                self.albums[artist.id].append(album)
                self.songs[album_id] = [
                    Song(
                        id=f"s{album_n}-{ti}", title=title, artist=band, album=album_name,
                        album_id=album_id, artist_id=artist.id, track=ti + 1,
                        year=album.year, duration=147 + (ti * 67) % 190,
                        cover_art=cover, suffix="flac", bit_rate=1017,
                        starred=(ti + album_n) % 5 == 0,
                    )
                    for ti, title in enumerate(TRACKS[album_n % 4 : album_n % 4 + 6])
                ]
                album_n += 1

    # ── api surface ───────────────────────────────────────────────────
    async def ping(self):
        return {"type": "navidrome", "serverVersion": "0.62.0"}

    async def get_artists(self):
        return self.artists

    async def get_artist_albums(self, artist_id):
        return self.albums.get(artist_id, [])

    async def get_album_songs(self, album_id):
        return self.songs.get(album_id, [])

    async def get_album_list(self, list_type, size=500, offset=0):
        every = [a for albums in self.albums.values() for a in albums]
        return every if list_type != "starred" else [a for a in every if a.starred]

    # playlists persist in memory so edits (rename/remove/reorder/delete)
    # round-trip like a real server; the defaults match the posed demo shots
    def _seed_playlists(self):
        if not hasattr(self, "_playlists"):
            self._playlists = {
                "pl1": Playlist(id="pl1", name="late night coding", song_count=14, duration=3300, owner="gheat"),
                "pl2": Playlist(id="pl2", name="gym (do not judge)", song_count=9, duration=2100, owner="gheat"),
            }
            self._playlist_songs = {
                "pl1": list(self.songs["al0"] + self.songs["al2"][:3]),
                "pl2": list(self.songs["al1"]),
            }

    async def get_playlists(self):
        self._seed_playlists()
        return list(self._playlists.values())

    async def get_podcasts(self):
        # two channels, a handful of episodes each — episodes are plain Songs
        # keyed on their streamId, so they play like any track
        channels = []
        for ci, (title, cover) in enumerate(
            [("Terminal Velocity", "cov0"), ("Reverb & Static", "cov3")]
        ):
            channel = PodcastChannel(id=f"pc{ci}", title=title, cover_art=cover, episode_count=3)
            episodes = [
                Song(
                    id=f"ep{ci}-{ei}", title=f"Ep {ei + 1}: {name}", artist=title,
                    album=title, duration=1800 + ei * 420, cover_art=cover,
                )
                for ei, name in enumerate(["The Cold Open", "Deep Dive", "Listener Mail"])
            ]
            channels.append((channel, episodes))
        return channels

    async def get_internet_radio_stations(self):
        # direct-URL stations: stream_url is set so mpv opens them straight
        return [
            Song(id="radio:r1", title="Lofi Beats FM", artist="internet radio",
                 stream_url=str(self._tone)),
            Song(id="radio:r2", title="Synthwave Nightdrive", artist="internet radio",
                 stream_url=str(self._tone)),
        ]

    async def get_playlist_songs(self, playlist_id):
        self._seed_playlists()
        return list(self._playlist_songs.get(playlist_id, []))

    async def get_starred(self):
        every_song = [s for songs in self.songs.values() for s in songs]
        every_album = [a for albums in self.albums.values() for a in albums]
        return SearchResults(
            artists=[a for a in self.artists if a.starred],
            albums=[a for a in every_album if a.starred],
            songs=[s for s in every_song if s.starred],
        )

    async def search(self, query, limit=20):
        q = query.lower()
        every_song = [s for songs in self.songs.values() for s in songs]
        every_album = [a for albums in self.albums.values() for a in albums]
        return SearchResults(
            artists=[a for a in self.artists if q in a.name.lower()],
            albums=[a for a in every_album if q in a.name.lower()],
            songs=[s for s in every_song if q in s.title.lower()],
        )

    async def get_genres(self):
        from navitui.models import Genre
        every_song = [s for songs in self.songs.values() for s in songs]
        return [
            Genre(name="shoegaze", song_count=len(every_song) // 2, album_count=4),
            Genre(name="post-rock", song_count=len(every_song) // 3, album_count=3),
            Genre(name="ambient", song_count=len(every_song) // 5, album_count=2),
        ]

    async def get_songs_by_genre(self, genre, count=500):
        return (self.songs["al0"] + self.songs["al1"])[:count]

    async def get_bookmarks(self):
        from navitui.models import Bookmark
        return [
            Bookmark(song=self.songs["al0"][0], position_ms=63000, comment="ch. 3"),
            Bookmark(song=self.songs["al2"][1], position_ms=1200_000, comment=""),
        ]

    async def create_bookmark(self, song_id, position_ms, comment=None):
        pass

    async def delete_bookmark(self, song_id):
        pass

    async def get_random_songs(self, size=50):
        return self.songs["al1"]

    async def get_similar_songs(self, item_id, count=20):
        # a plausible "similar" mix: a couple of other albums' tracks
        return (self.songs["al2"] + self.songs["al3"])[:count]

    async def get_top_songs(self, artist, count=20):
        return self.songs["al0"][:count]

    async def scrobble(self, song_id, submission):
        pass

    async def set_star(self, item_id, kind, star):
        pass

    async def get_all_songs(self, max_songs=5000):
        return [s for songs in self.songs.values() for s in songs][:max_songs]

    async def get_songs_by_albums(self, list_type, albums=15):
        album_list = await self.get_album_list(list_type)
        out = []
        for a in album_list[:albums]:
            out.extend(self.songs[a.id])
        return out

    def _find_song(self, song_id):
        for songs in self.songs.values():
            for s in songs:
                if s.id == song_id:
                    return s
        return None

    async def create_playlist(self, name, song_ids):
        self._seed_playlists()
        pid = f"pl{len(self._playlists) + 1}"
        entries = [s for sid in song_ids if (s := self._find_song(sid))]
        self._playlists[pid] = Playlist(
            id=pid, name=name, song_count=len(entries), owner="gheat"
        )
        self._playlist_songs[pid] = entries

    async def add_to_playlist(self, playlist_id, song_ids):
        self._seed_playlists()
        entries = [s for sid in song_ids if (s := self._find_song(sid))]
        self._playlist_songs.setdefault(playlist_id, []).extend(entries)
        if playlist_id in self._playlists:
            self._playlists[playlist_id].song_count = len(self._playlist_songs[playlist_id])

    async def remove_from_playlist(self, playlist_id, indices):
        self._seed_playlists()
        songs = self._playlist_songs.get(playlist_id, [])
        drop = set(indices)
        self._playlist_songs[playlist_id] = [s for i, s in enumerate(songs) if i not in drop]
        if playlist_id in self._playlists:
            self._playlists[playlist_id].song_count = len(self._playlist_songs[playlist_id])

    async def rename_playlist(self, playlist_id, name):
        self._seed_playlists()
        if playlist_id in self._playlists:
            self._playlists[playlist_id].name = name

    async def delete_playlist(self, playlist_id):
        self._seed_playlists()
        self._playlists.pop(playlist_id, None)
        self._playlist_songs.pop(playlist_id, None)

    async def reorder_playlist(self, playlist_id, song_ids):
        self._seed_playlists()
        by_id = {s.id: s for s in self._playlist_songs.get(playlist_id, [])}
        self._playlist_songs[playlist_id] = [by_id[i] for i in song_ids if i in by_id]

    async def set_rating(self, song_id, rating):
        pass

    async def get_lyrics(self, artist, title):
        return "La la la\nDemo lyrics, verse two\nLa la la"

    async def get_synced_lyrics(self, song_id):
        return [
            (0.0, "La la la"),
            (2.5, "Demo lyrics, verse two"),
            (5.0, "the highlight rides the beat"),
            (7.5, "La la la"),
        ]

    async def create_share(self, item_id):
        return f"https://demo.music.example/share/{item_id}"

    def stream_url(self, song_id):
        return str(self._tone)

    def cached_stream(self, song_id):
        # pretend a couple of tracks are already pinned so the ✓ marker shows
        return self._tone if song_id in ("s0-0", "s0-1") else None

    async def download_song(self, song_id):
        return self._tone

    def cached_art(self, cover_id, size=1200):
        return self._art_dir / f"{cover_id}-600"  # one size fits the fake

    async def cover_art(self, cover_id, size=1200):
        return self._art_dir / f"{cover_id}-600"

    async def close(self):
        pass


async def shoot_main() -> None:
    """Main view + search + void theme, one app session."""
    from navitui.app import NaviTuiApp

    app = NaviTuiApp(client=FakeClient(), ao="null")
    async with app.run_test(size=(132, 38)) as pilot:
        await pilot.pause(1.2)                 # sidebar + all-tracks settle
        app.query_one("#tracks-list").focus()
        await pilot.pause(0.3)
        await pilot.press("j", "j", "enter")   # play track 3
        await pilot.pause(2.5)
        await pilot.press("a")                 # queue one more
        await pilot.pause(1.2)
        app.save_screenshot(str(ASSETS / "main.svg"))

        await pilot.press("slash")
        await pilot.pause(0.3)
        for ch in "light":
            await pilot.press(ch)
        await pilot.pause(0.8)
        app.save_screenshot(str(ASSETS / "search.svg"))
        await pilot.press("escape")

        await pilot.press("t")                 # mocha -> void
        await pilot.pause(0.8)
        app.save_screenshot(str(ASSETS / "void.svg"))
        await app.run_action("quit")


async def shoot_onboarding() -> None:
    from navitui.app import NaviTuiApp
    from navitui.screens import OnboardingScreen

    app = NaviTuiApp(ao="null")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.6)
        assert isinstance(app.screen, OnboardingScreen)
        app.screen.query_one("#in-server").value = "https://music.example.com"
        app.screen.query_one("#in-user").value = "gheat"
        await pilot.pause(0.4)
        app.save_screenshot(str(ASSETS / "onboarding.svg"))
        app.exit()


def main() -> None:
    # each phase runs in its own subprocess: textual's test harness plus a
    # constantly-animating app can wedge when two sessions share a process
    if len(sys.argv) > 1:
        ASSETS.mkdir(exist_ok=True)
        asyncio.run(shoot_main() if sys.argv[1] == "main" else shoot_onboarding())
        shutil.rmtree(FAKE_HOME, ignore_errors=True)
        return

    for phase in ("main", "onboarding"):
        subprocess.run([sys.executable, __file__, phase], check=True, timeout=180)
    for name in ("main", "search", "void", "onboarding"):
        svg = ASSETS / f"{name}.svg"
        png = svg.with_suffix(".png")
        try:
            subprocess.run(["rsvg-convert", "--zoom", "1.6", "-o", str(png), str(svg)], check=True)
            print(f"  {png.relative_to(REPO)}")
        except (FileNotFoundError, subprocess.CalledProcessError):
            print(f"  {svg.relative_to(REPO)} (install rsvg-convert for PNGs)")


if __name__ == "__main__":
    main()
