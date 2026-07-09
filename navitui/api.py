"""Async Subsonic/OpenSubsonic client for Navidrome.

Auth is the salted-token scheme: we store md5(password + salt) and the salt,
never the password itself. All calls go through `_get`, which unwraps the
`subsonic-response` envelope and raises `SubsonicError` on failure.

Cover art is fetched once and kept as files under the app cache dir, so art
for anything you've already looked at renders instantly and offline. The same
cache-first pattern extends to audio: `download_song` pins the original file
next to the art, and `cached_stream` reports what is already on disk so
playback can prefer a local copy over the network (see `SubsonicClient`).
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
    def __init__(
        self,
        server: str,
        username: str,
        token: str,
        salt: str,
        art_dir: Path,
        audio_dir: Path | None = None,
        max_bitrate: int = 0,
        stream_format: str = "",
    ) -> None:
        self.server = normalize_server(server)
        self.username = username
        self._token = token
        self._salt = salt
        # Streaming transcode cap (network only — downloads pin originals).
        # Mutable so a runtime toggle can retune later streams; a track that is
        # already playing keeps its URL until the next `stream_url` call.
        self.max_bitrate = max_bitrate
        self.stream_format = stream_format
        self._art_dir = art_dir
        # audio pins live next to the art cache; defaults beside it so callers
        # that only pass art_dir still get a sane location
        self._audio_dir = audio_dir if audio_dir is not None else art_dir.parent / "audio"
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

    async def remove_from_playlist(self, playlist_id: str, indices: list[int]) -> None:
        """Drop entries by their zero-based position (updatePlaylist's
        songIndexToRemove — indices into the playlist as it stands server-side)."""
        await self._get("updatePlaylist", playlistId=playlist_id, songIndexToRemove=indices)

    async def rename_playlist(self, playlist_id: str, name: str) -> None:
        await self._get("updatePlaylist", playlistId=playlist_id, name=name)

    async def delete_playlist(self, playlist_id: str) -> None:
        await self._get("deletePlaylist", id=playlist_id)

    async def reorder_playlist(self, playlist_id: str, song_ids: list[str]) -> None:
        """Persist an arbitrary new song order. Subsonic's updatePlaylist has no
        move/reorder verb, so we rebuild the entry list in one call: clear every
        current entry (songIndexToRemove for 0..n-1) and re-add the ids in the
        desired order (songIdToAdd), both in the same request. Sending them
        together is atomic server-side — removes apply first, then the adds — so
        the playlist never briefly empties and metadata (name/owner) is kept."""
        remove = list(range(len(song_ids)))
        await self._get(
            "updatePlaylist",
            playlistId=playlist_id,
            songIndexToRemove=remove,
            songIdToAdd=song_ids,
        )

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

    async def get_similar_songs(self, item_id: str, count: int = 20) -> list[Song]:
        """Songs similar to a song/artist id (the seed for endless radio).
        Prefers OpenSubsonic's `getSimilarSongs2`, falling back to the older
        `getSimilarSongs` — servers without either just return an empty list."""
        for endpoint, key in (("getSimilarSongs2", "similarSongs2"), ("getSimilarSongs", "similarSongs")):
            try:
                body = await self._get(endpoint, id=item_id, count=count)
            except SubsonicError:
                continue
            songs = [Song.from_api(s) for s in body.get(key, {}).get("song", [])]
            if songs:
                return songs
        return []

    async def get_top_songs(self, artist: str, count: int = 20) -> list[Song]:
        """An artist's top tracks by name (`getTopSongs`) — a decent radio
        seed when similar-songs comes back empty."""
        body = await self._get("getTopSongs", artist=artist, count=count)
        return [Song.from_api(s) for s in body.get("topSongs", {}).get("song", [])]

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
        # maxBitRate/format only when set — 0/"" means original quality, so we
        # omit them and let the server serve the untranscoded file. Downloads
        # go through `download_song`, which never touches these, so offline
        # pins stay full quality regardless of the streaming cap.
        extra = {}
        if self.max_bitrate:
            extra["maxBitRate"] = self.max_bitrate
        if self.stream_format:
            extra["format"] = self.stream_format
        params = "&".join(f"{k}={v}" for k, v in self._params(id=song_id, **extra).items())
        return f"{self.server}/rest/stream?{params}"

    async def scrobble(self, song_id: str, submission: bool) -> None:
        await self._get("scrobble", id=song_id, submission="true" if submission else "false")

    async def set_star(self, item_id: str, kind: str, star: bool) -> None:
        """kind: song | album | artist"""
        key = {"song": "id", "album": "albumId", "artist": "artistId"}[kind]
        await self._get("star" if star else "unstar", **{key: item_id})

    async def set_rating(self, song_id: str, rating: int) -> None:
        """rating 1-5, or 0 to clear."""
        await self._get("setRating", id=song_id, rating=max(0, min(5, rating)))

    async def get_lyrics(self, artist: str, title: str) -> str:
        body = await self._get("getLyrics", artist=artist, title=title)
        return body.get("lyrics", {}).get("value", "") or ""

    async def get_synced_lyrics(self, song_id: str) -> list[tuple[float, str]] | None:
        """Timed lyrics for a song via OpenSubsonic `getLyricsBySongId`.

        Returns a list of (start_seconds, text) lines when the server has
        SYNCED structured lyrics, else None (no endpoint, unsynced only, or
        nothing found) so the caller can fall back to plain `get_lyrics`.
        """
        try:
            body = await self._get("getLyricsBySongId", id=song_id)
        except (SubsonicError, httpx.HTTPError):
            return None  # server lacks the endpoint / no match — fall back
        structured = body.get("lyricsList", {}).get("structuredLyrics", [])
        # prefer a synced track; ignore unsynced ones (plain path handles them)
        for track in structured:
            if not track.get("synced"):
                continue
            lines: list[tuple[float, str]] = []
            for line in track.get("line", []):
                start = line.get("start")
                if start is None:
                    continue
                lines.append((start / 1000.0, line.get("value", "") or ""))
            if lines:
                lines.sort(key=lambda l: l[0])
                return lines
        return None

    async def create_share(self, item_id: str) -> str:
        """Public share link for a song/album (needs sharing enabled
        server-side). Returns the URL."""
        body = await self._get("createShare", id=item_id)
        shares = body.get("shares", {}).get("share", [])
        if not shares or not shares[0].get("url"):
            raise SubsonicError("server did not return a share url")
        return shares[0]["url"]

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

    # ── audio cache (offline pins) ────────────────────────────────────
    # Original files are pinned by id under the audio cache dir. We keep the
    # id opaque (no suffix in the name): the server may transcode or the
    # suffix may be unknown offline, and mpv probes the container regardless.
    # `download_song` mirrors `cover_art` exactly — atomic .part→rename with
    # the JSON-error content-type guard — so a killed download never leaves a
    # half file that reads as "downloaded".
    def _audio_path(self, song_id: str) -> Path:
        return self._audio_dir / song_id.replace("/", "_")

    def cached_stream(self, song_id: str) -> Path | None:
        """The pinned file for this song, or None. Cheap exists-check that the
        UI and offline-first playback lean on (like `cached_art`)."""
        path = self._audio_path(song_id)
        return path if path.exists() else None

    async def download_song(self, song_id: str) -> Path:
        """Pin the ORIGINAL file for `song_id` and return its path. Idempotent:
        an already-pinned song returns instantly. Fetches the `download`
        endpoint (untranscoded original) and falls back to `stream`."""
        path = self._audio_path(song_id)
        if path.exists():
            return path
        resp = await self._fetch_audio("download", song_id)
        if resp is None:
            resp = await self._fetch_audio("stream", song_id)
        if resp is None:
            raise SubsonicError("could not download song")
        self._audio_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        tmp.write_bytes(resp.content)
        tmp.replace(path)
        return path

    async def _fetch_audio(self, endpoint: str, song_id: str):
        """GET an audio endpoint, returning the response or None on any error
        (including the JSON error envelope some servers send instead of 4xx)."""
        try:
            resp = await self._http.get(
                f"{self.server}/rest/{endpoint}", params=self._params(id=song_id)
            )
            resp.raise_for_status()
        except httpx.HTTPError:
            return None
        if resp.headers.get("content-type", "").startswith("application/json"):
            return None
        return resp

    def remove_pin(self, song_id: str) -> bool:
        """Drop a pinned file (used by eviction / a future downloads panel).
        Returns True if something was removed."""
        path = self._audio_path(song_id)
        try:
            path.unlink()
            return True
        except OSError:
            return False
