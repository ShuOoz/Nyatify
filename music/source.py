"""Turn a request into an Apple Music, YouTube, Spotify, SoundCloud, or NetEase track."""

from __future__ import annotations

import logging
from urllib.parse import urlparse

import wavelink

log = logging.getLogger(__name__)

# A link is sent only when the host is one of these. Lavalink then picks the
# source. A song name uses the platform the command asked for.
_HOSTS = {
    "music.apple.com": "apple",
    "youtube.com": "youtube",
    "m.youtube.com": "youtube",
    "music.youtube.com": "youtube",
    "youtu.be": "youtube",
    "open.spotify.com": "spotify",
    "spotify.link": "spotify",
    "soundcloud.com": "soundcloud",
    "music.163.com": "netease",
}
# Share links use another host. Lavalink wants the desktop host.
_ALIASES = {
    "geo.music.apple.com": "music.apple.com",
    "y.music.163.com": "music.163.com",
    "m.soundcloud.com": "soundcloud.com",
}
# These hosts only redirect. The address they land on is what gets played.
_SHORT_HOSTS = {"on.soundcloud.com"}
_SEARCH = {
    "apple": "amsearch",
    "youtube": wavelink.TrackSource.YouTube,
    "spotify": "spsearch",
    "soundcloud": "scsearch",
    "netease": "ntsearch",
}
_LABELS = {
    "apple": "Apple Music",
    "youtube": "YouTube",
    "spotify": "Spotify",
    "soundcloud": "SoundCloud",
    "netease": "NetEase",
}

# A song name starts on Apple Music. The card can switch. A link keeps its own site.
NAME_PLATFORM = "apple"

# A name keeps this many songs. The card shows five at a time.
MATCHES = 25

# SoundCloud Go+ search hits are this long. The full song is not in the result.
PREVIEW_MS = 30_000

# The label is the button. The phrase is a style Spotify is searched for.
# A bare genre word matches song titles, so every phrase is more than one word.
GENRES = (
    ("Jazz", "jazz piano"),
    ("Classical", "classical piano"),
    ("Lo-fi", "lofi hip hop"),
    ("Rock", "classic rock"),
    ("Pop", "dance pop"),
    ("Electronic", "electronic dance"),
    ("Hip hop", "hip hop rap"),
    ("R&B", "r&b soul"),
    ("Ambient", "ambient instrumental"),
)

# The label is the list. A pick starts on one of five long uploads, chosen at random.
# When that video ends, playback walks forward through the five.
# These are ordinary videos, hours long. A livestream dies after a few seconds.
RADIOS = (
    ("Lo-fi", "lofi"),
    ("Jazz", "jazz"),
    ("Classical", "classical"),
    ("Ambient", "ambient"),
    ("Electronic", "electronic"),
)

_RADIO_LIVES = {
    "lofi": ("DfpdnFjfRvo", "eTP5PZ8NoeU", "FuCrTDiYRj0", "8b3fqIBrNW0", "iJm5cdWbh84"),
    "jazz": ("lj4VncsqFQw", "4ZUhtr_3o7c", "BywDOO99Ia0", "x0cEyeO5V6s", "uHo0CaawTqs"),
    "classical": ("WLWJy1eXX2c", "E2LM3ZlcDnk", "shoVsQhou-8", "BI8Y44J6MgI", "nBPnvGLD6BA"),
    "ambient": ("yUXwdnOJ_z0", "LA_JmZTuQLo", "6zxNSmCwVlk", "2iEBB0cGrFY", "tLeoq4hhI00"),
    "electronic": ("F7wlQMx37yI", "a47T2qylqWU", "QM7L27N8-rE", "IJflpkRk_eU", "byGPEiHbsnc"),
}

def radio_lives(key: str) -> tuple[str, ...]:
    """Five long videos for one station, in play order."""
    return tuple(
        f"https://www.youtube.com/watch?v={video_id}"
        for video_id in _RADIO_LIVES.get(key, ())
    )


def normalize_platform(name: str | None) -> str:
    key = (name or "").strip().lower()
    return key if key in _SEARCH else "youtube"


def platform_key(query: str, platform: str = "youtube") -> str:
    parsed = urlparse(query)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if host == "m.soundcloud.com":
        host = "soundcloud.com"
    return _HOSTS.get(host, normalize_platform(platform))


def platform_label(query: str, platform: str = "youtube") -> str:
    return _LABELS[platform_key(query, platform)]


def platform_pairs() -> tuple[tuple[str, str], ...]:
    return tuple((key, _LABELS[key]) for key in _SEARCH)


def platform_line(*, last: str = "and") -> str:
    """The five sites in menu order, joined for a sentence."""
    labels = [label for _, label in platform_pairs()]
    if len(labels) < 2:
        return labels[0] if labels else ""
    return f"{', '.join(labels[:-1])}, {last} {labels[-1]}"


def is_link(query: str) -> bool:
    parsed = urlparse(query)
    return bool(parsed.scheme and parsed.netloc)


def is_playlist_link(query: str) -> bool:
    """A playlist or a SoundCloud set.

    A watch link can carry a list id. That link is still the one recording.
    """
    if not is_link(query):
        return False
    path = (urlparse(query).path or "").lower()
    place = _place(query)
    if "/playlist" in place:
        return True
    return "/sets/" in path or path.rstrip("/").endswith("/sets")


def is_album_link(query: str) -> bool:
    return is_link(query) and "/album" in _place(query)


def is_artist_link(query: str) -> bool:
    return is_link(query) and "/artist" in _place(query)


def request_mode(query: str) -> str:
    if not is_link(query):
        return "name"
    if is_playlist_link(query) or is_album_link(query):
        return "playlist"
    if is_artist_link(query):
        return "artist"
    return "track"


def _place(query: str) -> str:
    parsed = urlparse(query)
    path = (parsed.path or "").lower()
    return f"{path}#{(parsed.fragment or '').lower()}"


def drop_previews(tracks: list, *, search: bool = True) -> list:
    """Leave out SoundCloud previews.

    A search treats a 30-second hit as a preview. A direct link plays that length.
    """
    return [track for track in tracks if not _preview(track, search=search)]


def _preview(track: object, *, search: bool) -> bool:
    if getattr(track, "is_preview", None) is True:
        return True
    if not search:
        return False
    length = getattr(track, "length", None)
    if isinstance(length, bool) or not isinstance(length, int):
        return False
    return 0 < length <= PREVIEW_MS


def take_tracks(results: object, mode: str) -> list:
    """A name keeps the first matches. A song link keeps the recording. A playlist keeps every song."""
    tracks = getattr(results, "tracks", None)
    if isinstance(tracks, list):
        found = list(tracks)
    elif isinstance(results, list):
        found = list(results)
    else:
        found = []
    if mode in ("playlist", "draw"):
        return found
    if mode == "track":
        return found[:1]
    if mode == "artist":
        return found[:MATCHES]
    return found[:MATCHES]


def track_query(text: str) -> str | None:
    """A song name or an Apple Music, YouTube, Spotify, SoundCloud, or NetEase link.

    None means the text is a link from somewhere else. An empty string is blank.
    """
    query = " ".join(text.split())
    if not query:
        return ""
    parsed = urlparse(query)
    if not parsed.scheme and not parsed.netloc:
        head = query.split("/", 1)[0].split("?", 1)[0].lower().removeprefix("www.")
        if head not in _HOSTS and head not in _ALIASES and head not in _SHORT_HOSTS:
            return query
        query = f"https://{query}"
        parsed = urlparse(query)
    host = _host(parsed)
    if host in _ALIASES:
        parsed = parsed._replace(netloc=_ALIASES[host])
        query = parsed.geturl()
        host = _ALIASES[host]
    if host in _HOSTS or host in _SHORT_HOSTS:
        return query
    return None


def needs_redirect(query: str) -> bool:
    return _host(urlparse(query)) in _SHORT_HOSTS


async def follow_redirect(url: str) -> str | None:
    """The page a short share link lands on, when that page is a supported link."""
    try:
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, allow_redirects=True) as response:
                final = str(response.url)
    except Exception:
        log.warning("music link did not open", exc_info=True)
        return None
    opened = track_query(final)
    if not opened or needs_redirect(opened):
        return None
    return opened


def _host(parsed: object) -> str:
    return (getattr(parsed, "hostname", None) or "").lower().removeprefix("www.")


async def search_track(query: str, platform: str = "youtube"):
    parsed = urlparse(query)
    if parsed.scheme and parsed.netloc:
        return await wavelink.Playable.search(query)
    source = _SEARCH[normalize_platform(platform)]
    return await wavelink.Playable.search(query, source=source)
