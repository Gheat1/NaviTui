"""Domain models — thin dataclasses over the Subsonic JSON shapes.

Every model round-trips through plain dicts (`to_dict`/`from_dict`) so the
whole library can live in the AppDirs JSON cache and render instantly on
the next launch.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Artist:
    id: str
    name: str
    album_count: int = 0
    cover_art: str | None = None
    starred: bool = False

    @classmethod
    def from_api(cls, d: dict) -> "Artist":
        return cls(
            id=str(d["id"]),
            name=d.get("name", "?"),
            album_count=int(d.get("albumCount", 0) or 0),
            cover_art=d.get("coverArt"),
            starred=bool(d.get("starred")),
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Artist":
        return cls(**d)


@dataclass
class Album:
    id: str
    name: str
    artist: str = ""
    artist_id: str | None = None
    year: int | None = None
    song_count: int = 0
    duration: int = 0
    cover_art: str | None = None
    starred: bool = False

    @classmethod
    def from_api(cls, d: dict) -> "Album":
        return cls(
            id=str(d["id"]),
            name=d.get("name") or d.get("title") or "?",
            artist=d.get("artist", ""),
            artist_id=str(d["artistId"]) if d.get("artistId") else None,
            year=d.get("year"),
            song_count=int(d.get("songCount", 0) or 0),
            duration=int(d.get("duration", 0) or 0),
            cover_art=d.get("coverArt"),
            starred=bool(d.get("starred")),
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Album":
        return cls(**d)


@dataclass
class Song:
    id: str
    title: str
    artist: str = ""
    album: str = ""
    album_id: str | None = None
    artist_id: str | None = None
    track: int | None = None
    disc: int | None = None
    year: int | None = None
    duration: int = 0
    cover_art: str | None = None
    suffix: str = ""
    bit_rate: int | None = None
    starred: bool = False
    user_rating: int = 0

    @classmethod
    def from_api(cls, d: dict) -> "Song":
        return cls(
            id=str(d["id"]),
            title=d.get("title", "?"),
            artist=d.get("artist", ""),
            album=d.get("album", ""),
            album_id=str(d["albumId"]) if d.get("albumId") else None,
            artist_id=str(d["artistId"]) if d.get("artistId") else None,
            track=d.get("track"),
            disc=d.get("discNumber"),
            year=d.get("year"),
            duration=int(d.get("duration", 0) or 0),
            cover_art=d.get("coverArt"),
            suffix=d.get("suffix", ""),
            bit_rate=d.get("bitRate"),
            starred=bool(d.get("starred")),
            user_rating=int(d.get("userRating", 0) or 0),
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Song":
        return cls(**d)


@dataclass
class Playlist:
    id: str
    name: str
    song_count: int = 0
    duration: int = 0
    owner: str = ""

    @classmethod
    def from_api(cls, d: dict) -> "Playlist":
        return cls(
            id=str(d["id"]),
            name=d.get("name", "?"),
            song_count=int(d.get("songCount", 0) or 0),
            duration=int(d.get("duration", 0) or 0),
            owner=d.get("owner", ""),
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Playlist":
        return cls(**d)


@dataclass
class Genre:
    name: str
    song_count: int = 0
    album_count: int = 0

    @classmethod
    def from_api(cls, d: dict) -> "Genre":
        # getGenres returns the name in "value"; browse filters use it verbatim
        return cls(
            name=d.get("value") or d.get("name") or "?",
            song_count=int(d.get("songCount", 0) or 0),
            album_count=int(d.get("albumCount", 0) or 0),
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Genre":
        return cls(**d)


@dataclass
class Bookmark:
    """A saved resume point in a track — the Song plus a position in ms and
    an optional comment. Round-trips through the cache like everything else."""

    song: Song
    position_ms: int = 0
    comment: str = ""

    @classmethod
    def from_api(cls, d: dict) -> "Bookmark":
        return cls(
            song=Song.from_api(d.get("entry", {})),
            position_ms=int(d.get("position", 0) or 0),
            comment=d.get("comment", "") or "",
        )

    def to_dict(self) -> dict:
        return {
            "song": self.song.to_dict(),
            "position_ms": self.position_ms,
            "comment": self.comment,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Bookmark":
        return cls(
            song=Song.from_dict(d["song"]),
            position_ms=int(d.get("position_ms", 0) or 0),
            comment=d.get("comment", "") or "",
        )


@dataclass
class SearchResults:
    artists: list[Artist] = field(default_factory=list)
    albums: list[Album] = field(default_factory=list)
    songs: list[Song] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.artists or self.albums or self.songs)
