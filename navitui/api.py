"""Async Subsonic/OpenSubsonic client for Navidrome.

Auth is the salted-token scheme: we store md5(password + salt) and the salt,
never the password itself. All calls go through `_get`, which unwraps the
`subsonic-response` envelope and raises `SubsonicError` on failure.

Cover art is fetched once and kept as files under the app cache dir, so art
for anything you've already looked at renders instantly and offline.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from pathlib import Path

import httpx

from navitui.models import Album, Artist, Playlist, SearchResults, Song

API_VERSION = "1.16.1"
CLIENT_NAME = "navitui"


class SubsonicError(Exception):
    """Server said no (bad auth, missing id, …)."""


def make_token(password: str) -> tuple[str, str]:
    """Return (token, salt) for the salted md5 auth scheme."""
    salt = secrets.token_hex(8)
    token = hashlib.md5((password + salt).encode()).hexdigest()
    return token, salt


def normalize_server(url: str) -> str:
    url = url.strip().rstrip("/")
    if url and "://" not in url:
        url = "https://" + url
    return url


class SubsonicClient:
    def __init__(self, server: str, username: str, token: str, salt: str, art_dir: Path) -> None:
        self.server = normalize_server(server)
        self.username = username
        self._token = token
        self._salt = salt
        self._art_dir = art_dir
        self._http = httpx.AsyncClient(timeout=20, follow_redirects=True)

    async def close(self) -> None:
        await self._http.aclose()

    # ── plumbing ──────────────────────────────────────────────────────
    def _params(self, **extra) -> dict:
        params = {
            "u": self.username,
            "t": self._token,
            "s": self._salt,
            "v": API_VERSION,
            "c": CLIENT_NAME,
            "f": "json",
        }
        params.update({k: v for k, v in extra.items() if v is not None})
        return params

    async def _get(self, endpoint: str, **params) -> dict:
        url = f"{self.server}/rest/{endpoint}"
        resp = await self._http.get(url, params=self._params(**params))
        resp.raise_for_status()
        body = resp.json().get("subsonic-response", {})
        if body.get("status") != "ok":
            err = body.get("error", {})
            raise SubsonicError(err.get("message", f"error {err.get('code', '?')}"))
        return body

    # ── library ───────────────────────────────────────────────────────
    async def ping(self) -> dict:
        return await self._get("ping")

    async def get_artists(self) -> list[Artist]:
        body = await self._get("getArtists")
        artists: list[Artist] = []
        for index in body.get("artists", {}).get("index", []):
            for a in index.get("artist", []):
                artists.append(Artist.from_api(a))
        return artists

    async def get_artist_albums(self, artist_id: str) -> list[Album]:
        body = await self._get("getArtist", id=artist_id)
        return [Album.from_api(a) for a in body.get("artist", {}).get("album", [])]

    async def get_album_songs(self, album_id: str) -> list[Song]:
        body = await self._get("getAlbum", id=album_id)
        return [Song.from_api(s) for s in body.get("album", {}).get("song", [])]

    async def get_album_list(self, list_type: str, size: int = 500, offset: int = 0) -> list[Album]:
        """list_type: newest | recent | frequent | random | starred | alphabeticalByName"""
        body = await self._get("getAlbumList2", type=list_type, size=size, offset=offset)
        return [Album.from_api(a) for a in body.get("albumList2", {}).get("album", [])]

    async def get_playlists(self) -> list[Playlist]:
        body = await self._get("getPlaylists")
        return [Playlist.from_api(p) for p in body.get("playlists", {}).get("playlist", [])]

    async def create_playlist(self, name: str, song_ids: list[str]) -> None:
        await self._get("createPlaylist", name=name, songId=song_ids)

    async def add_to_playlist(self, playlist_id: str, song_ids: list[str]) -> None:
        await self._get("updatePlaylist", playlistId=playlist_id, songIdToAdd=song_ids)

    async def get_playlist_songs(self, playlist_id: str) -> list[Song]:
        body = await self._get("getPlaylist", id=playlist_id)
        return [Song.from_api(s) for s in body.get("playlist", {}).get("entry", [])]

    async def get_starred(self) -> SearchResults:
        body = await self._get("getStarred2")
        starred = body.get("starred2", {})
        return SearchResults(
            artists=[Artist.from_api(a) for a in starred.get("artist", [])],
            albums=[Album.from_api(a) for a in starred.get("album", [])],
            songs=[Song.from_api(s) for s in starred.get("song", [])],
        )

    async def search(self, query: str, limit: int = 20) -> SearchResults:
        body = await self._get(
            "search3",
            query=query,
            artistCount=limit,
            albumCount=limit,
            songCount=limit * 2,
        )
        result = body.get("searchResult3", {})
        return SearchResults(
            artists=[Artist.from_api(a) for a in result.get("artist", [])],
            albums=[Album.from_api(a) for a in result.get("album", [])],
            songs=[Song.from_api(s) for s in result.get("song", [])],
        )

    async def get_random_songs(self, size: int = 50) -> list[Song]:
        body = await self._get("getRandomSongs", size=size)
        return [Song.from_api(s) for s in body.get("randomSongs", {}).get("song", [])]

    async def get_songs_by_albums(self, list_type: str, albums: int = 15) -> list[Song]:
        """Songs-first view of an album list: flatten the songs of the top N
        albums for `newest` / `recent` / `frequent`, keeping album order."""
        album_list = await self.get_album_list(list_type, size=albums)
        results = await asyncio.gather(
            *(self.get_album_songs(a.id) for a in album_list),
            return_exceptions=True,
        )
        songs: list[Song] = []
        for result in results:
            if isinstance(result, list):
                songs.extend(result)
        return songs

    async def get_all_songs(self, max_songs: int = 5000) -> list[Song]:
        """Every song in the library, paged through search3 with the empty
        query (the Navidrome/OpenSubsonic 'list everything' convention)."""
        songs: list[Song] = []
        page = 500
        while len(songs) < max_songs:
            body = await self._get(
                "search3",
                query='""',
                artistCount=0,
                albumCount=0,
                songCount=page,
                songOffset=len(songs),
            )
            batch = body.get("searchResult3", {}).get("song", [])
            songs.extend(Song.from_api(s) for s in batch)
            if len(batch) < page:
                break
        return songs[:max_songs]

    # ── playback side-channel ─────────────────────────────────────────
    def stream_url(self, song_id: str) -> str:
        params = "&".join(f"{k}={v}" for k, v in self._params(id=song_id).items())
        return f"{self.server}/rest/stream?{params}"

    async def scrobble(self, song_id: str, submission: bool) -> None:
        await self._get("scrobble", id=song_id, submission="true" if submission else "false")

    async def set_star(self, item_id: str, kind: str, star: bool) -> None:
        """kind: song | album | artist"""
        key = {"song": "id", "album": "albumId", "artist": "artistId"}[kind]
        await self._get("star" if star else "unstar", **{key: item_id})

    # ── cover art ─────────────────────────────────────────────────────
    # 1200px: big enough that kitty/sixel terminals get a crisp image at
    # any panel size; halfcell terminals are bounded by cells either way
    def cached_art(self, cover_id: str, size: int = 1200) -> Path | None:
        path = self._art_dir / f"{cover_id.replace('/', '_')}-{size}"
        return path if path.exists() else None

    async def cover_art(self, cover_id: str, size: int = 1200) -> Path:
        path = self._art_dir / f"{cover_id.replace('/', '_')}-{size}"
        if path.exists():
            return path
        resp = await self._http.get(
            f"{self.server}/rest/getCoverArt",
            params=self._params(id=cover_id, size=size),
        )
        resp.raise_for_status()
        if resp.headers.get("content-type", "").startswith("application/json"):
            body = resp.json().get("subsonic-response", {})
            err = body.get("error", {})
            raise SubsonicError(err.get("message", "no cover art"))
        self._art_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        tmp.write_bytes(resp.content)
        tmp.replace(path)
        return path
