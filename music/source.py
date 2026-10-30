"""Turn a request into an Apple Music, YouTube, Spotify, SoundCloud, or NetEase track."""

from __future__ import annotations

import logging
from urllib.parse import parse_qsl, unquote, urlencode, urlparse, urlunparse

import wavelink

log = logging.getLogger(__name__)

# Label, then the prefix Lavalink searches with. A link does not use the prefix.
_PLATFORMS = {
    "apple": ("Apple Music", "amsearch"),
    "youtube": ("YouTube", wavelink.TrackSource.YouTube),
    "spotify": ("Spotify", "spsearch"),
    "soundcloud": ("SoundCloud", "scsearch"),
    "netease": ("NetEase", "ntsearch"),
}

# Host to platform. The second value is the desktop host Lavalink wants.
# Short hosts only redirect; the page they land on is what gets played.
_SITES: dict[str, tuple[str, str | None]] = {
    "music.apple.com": ("apple", None),
    "geo.music.apple.com": ("apple", "music.apple.com"),
    "embed.music.apple.com": ("apple", "music.apple.com"),
    "youtube.com": ("youtube", None),
    "m.youtube.com": ("youtube", None),
    "music.youtube.com": ("youtube", None),
    "youtu.be": ("youtube", None),
    "open.spotify.com": ("spotify", None),
    "spotify.link": ("spotify", None),
    "soundcloud.com": ("soundcloud", None),
    "m.soundcloud.com": ("soundcloud", "soundcloud.com"),
    "on.soundcloud.com": ("soundcloud", None),
    "music.163.com": ("netease", None),
    "y.music.163.com": ("netease", "music.163.com"),
}
_SHORT = {"on.soundcloud.com", "spotify.link"}
_YOUTUBE = {host for host, (platform, _) in _SITES.items() if platform == "youtube"}
_SPOTIFY = {"track", "album", "playlist", "episode", "artist"}
_TRACKING = {"si", "feature", "pp", "fbclid"}

# A song name starts on Apple Music. The card can switch. A link keeps its own site.
NAME_PLATFORM = "apple"
MATCHES = 25
PREVIEW_MS = 30_000

# Spotify's own list of new songs in that genre. The button plays one of them.
GENRES = (
    ("Jazz", "https://open.spotify.com/playlist/37i9dQZF1DXcWL5K0oNHcG"),
    ("Classical", "https://open.spotify.com/playlist/37i9dQZF1DWV0gynK7G6pD"),
    ("Lo-fi", "https://open.spotify.com/playlist/37i9dQZF1DX3SNr5BeQZSd"),
    ("Rock", "https://open.spotify.com/playlist/37i9dQZF1DWZryfp6NSvtz"),
    ("Pop", "https://open.spotify.com/playlist/37i9dQZF1DX11otjJ7crqp"),
    ("Electronic", "https://open.spotify.com/playlist/37i9dQZF1DXa41CMuUARjl"),
    ("Hip hop", "https://open.spotify.com/playlist/37i9dQZF1DX4SrOBCjlfVi"),
    ("R&B", "https://open.spotify.com/playlist/37i9dQZF1DWUzFXarNiofw"),
    ("Ambient", "https://open.spotify.com/playlist/37i9dQZF1DX9c7yCloFHHL"),
)

# Five long uploads. A livestream dies after a few seconds, so these are ordinary videos.
RADIOS = (
    ("Lo-fi", "lofi"),
    ("Jazz", "jazz"),
    ("Classical", "classical"),
    ("Ambient", "ambient"),
    ("Electronic", "electronic"),
)
_STATION_VIDEOS = {
    "lofi": ("DfpdnFjfRvo", "eTP5PZ8NoeU", "FuCrTDiYRj0", "8b3fqIBrNW0", "iJm5cdWbh84"),
    "jazz": ("lj4VncsqFQw", "4ZUhtr_3o7c", "BywDOO99Ia0", "x0cEyeO5V6s", "uHo0CaawTqs"),
    "classical": ("WLWJy1eXX2c", "E2LM3ZlcDnk", "shoVsQhou-8", "BI8Y44J6MgI", "nBPnvGLD6BA"),
    "ambient": ("yUXwdnOJ_z0", "LA_JmZTuQLo", "6zxNSmCwVlk", "2iEBB0cGrFY", "tLeoq4hhI00"),
    "electronic": ("F7wlQMx37yI", "a47T2qylqWU", "QM7L27N8-rE", "IJflpkRk_eU", "byGPEiHbsnc"),
}


def station_videos(key: str) -> tuple[str, ...]:
    return tuple(f"https://www.youtube.com/watch?v={video_id}" for video_id in _STATION_VIDEOS.get(key, ()))


def normalize_platform(name: str | None) -> str:
    key = (name or "").strip().lower()
    return key if key in _PLATFORMS else "youtube"


def platform_key(query: str, platform: str = "youtube") -> str:
    found = _SITES.get(_host(urlparse(query)))
    return found[0] if found else normalize_platform(platform)


def platform_label(query: str, platform: str = "youtube") -> str:
    return _PLATFORMS[platform_key(query, platform)][0]


def platform_pairs() -> tuple[tuple[str, str], ...]:
    return tuple((key, label) for key, (label, _) in _PLATFORMS.items())


def platform_line(*, last: str = "and") -> str:
    labels = [label for label, _ in _PLATFORMS.values()]
    return f"{', '.join(labels[:-1])}, {last} {labels[-1]}"


def is_link(query: str) -> bool:
    parsed = urlparse(query)
    return bool(parsed.scheme and parsed.netloc)


def request_mode(query: str) -> str:
    """name, track, playlist (a playlist or an album), or artist."""
    if not is_link(query):
        return "name"
    host = _site(query)
    sections = _sections(query)
    # A SoundCloud title is a free slug. Only a real sets segment is a collection.
    if "sets" in sections or (host != "soundcloud.com" and "playlist" in sections):
        return "playlist"
    if host != "soundcloud.com" and "album" in sections and not _apple_song(query):
        return "playlist"
    if host != "soundcloud.com" and "artist" in sections:
        return "artist"
    return "track"


def import_link(query: str) -> str | None:
    """A playlist or album Create can save. A watch link saves its list, when it has one."""
    if request_mode(query) == "playlist":
        return query
    return _youtube_list(query)


def canonical_url(url: str) -> str:
    """One address for one recording. Tracking parameters such as si are left off."""
    video = _youtube_video(url)
    if video:
        return f"https://www.youtube.com/watch?v={video}"
    direct = _spotify_url(url) or _apple_song(url)
    if direct:
        return direct
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return url
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    kept = [(key, value) for key, value in pairs if not _tracking(key)]
    path = parsed.path.rstrip("/") if len(parsed.path) > 1 else parsed.path
    if kept == pairs and path == parsed.path and not parsed.fragment:
        return url
    return urlunparse(parsed._replace(path=path, query=urlencode(kept), fragment=""))


def track_query(text: str) -> str | None:
    """A song name or a supported link. None is some other site. An empty string is blank."""
    query = " ".join(text.split())
    if not query:
        return ""
    parsed = urlparse(query)
    if not parsed.scheme and not parsed.netloc:
        head = query.split("/", 1)[0].split("?", 1)[0].lower().removeprefix("www.")
        if head not in _SITES:
            return query
        query = f"https://{query}"
        parsed = urlparse(query)
    row = _SITES.get(_host(parsed))
    if row is None:
        return None
    desktop = row[1]
    if desktop:
        parsed = parsed._replace(netloc=desktop)
        query = parsed.geturl()
    return _spotify_url(query) or _apple_song(query) or query


def needs_redirect(query: str) -> bool:
    return _host(urlparse(query)) in _SHORT


async def follow_redirect(url: str) -> str | None:
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


def drop_previews(tracks: list, *, search: bool = True) -> list:
    """A search drops a 30-second SoundCloud hit. A direct link plays that length."""
    return [track for track in tracks if not _preview(track, search)]


def take_tracks(results: object, mode: str) -> list:
    """A name or an artist keeps the first matches. A song keeps one. A playlist keeps all."""
    raw = getattr(results, "tracks", None)
    if isinstance(raw, list):
        found = list(raw)
    elif isinstance(results, list):
        found = list(results)
    else:
        found = []
    if mode in ("playlist", "draw"):
        return found
    if mode != "track":
        return found[:MATCHES]
    picked = _selected(results)
    if picked is not None and picked < len(found):
        return [found[picked]]
    return found[:1]


async def search_track(query: str, platform: str = "youtube"):
    parsed = urlparse(query)
    if parsed.scheme and parsed.netloc:
        return await wavelink.Playable.search(query)
    _, prefix = _PLATFORMS[normalize_platform(platform)]
    return await wavelink.Playable.search(query, source=prefix)


def _host(parsed: object) -> str:
    return (getattr(parsed, "hostname", None) or "").lower().removeprefix("www.")


def _site(query: str) -> str:
    host = _host(urlparse(query))
    row = _SITES.get(host)
    if row is None:
        return host
    return row[1] or host


def _sections(query: str) -> set[str]:
    parsed = urlparse(query)
    found: set[str] = set()
    for chunk in (parsed.path or "", parsed.fragment or ""):
        for part in chunk.split("/"):
            head = part.split("?", 1)[0].split("&", 1)[0].strip().lower()
            if head:
                found.add(head)
    return found


def _query_value(query: str, key: str) -> str:
    return next((value.strip() for name, value in parse_qsl(query) if name == key and value.strip()), "")


def _tracking(key: str) -> bool:
    name = key.lower()
    return name in _TRACKING or name.startswith("utm_")


def _selected(results: object) -> int | None:
    index = getattr(results, "selected", None)
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        return None
    return index


def _youtube_video(url: str) -> str | None:
    parsed = urlparse(url)
    host = _host(parsed)
    if host not in _YOUTUBE:
        return None
    if host == "youtu.be":
        return parsed.path.strip("/").split("/", 1)[0] or None
    path = parsed.path.rstrip("/")
    for prefix in ("/shorts/", "/embed/", "/live/", "/v/"):
        if path.startswith(prefix):
            return path[len(prefix):].split("/", 1)[0] or None
    if path == "/watch":
        return _query_value(parsed.query, "v") or None
    return None


def _youtube_list(query: str) -> str | None:
    parsed = urlparse(query)
    if _host(parsed) not in _YOUTUBE:
        return None
    list_id = _query_value(parsed.query, "list")
    return f"https://www.youtube.com/playlist?list={list_id}" if list_id else None


def _spotify_url(url: str) -> str | None:
    parsed = urlparse(url)
    if _host(parsed) != "open.spotify.com":
        return None
    parts = [part for part in parsed.path.split("/") if part]
    while parts and (parts[0] == "embed" or parts[0].startswith("intl-")):
        parts = parts[1:]
    if len(parts) < 2 or parts[0] not in _SPOTIFY:
        return None
    return f"https://open.spotify.com/{parts[0]}/{parts[1]}"


def spotify_album(url: str) -> str | None:
    """The https album address. Spotify also sends spotify:album:{id}."""
    text = " ".join(url.split())
    if text.lower().startswith("spotify:album:"):
        ident = text.split(":", 2)[-1].split("?", 1)[0].strip()
        if ident and ident.lower() != "local":
            return f"https://open.spotify.com/album/{ident}"
    page = _spotify_url(text)
    if page and "/album/" in page:
        return page
    return None


def apple_album(url: str) -> tuple[str, str] | None:
    """Album name and address from an Apple song link.

    The song address is /album/{name}/{id}?i={song}. The album is that link without ?i=.
    """
    parsed = urlparse(url)
    if _site(url) != "music.apple.com":
        return None
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if len(parts) < 4 or len(parts[0]) != 2 or parts[1].lower() != "album":
        return None
    slug, album_id = parts[2], parts[3]
    if not album_id or slug.isdigit():
        return None
    name = " ".join(word.capitalize() for word in slug.replace("-", " ").split())
    address = f"https://music.apple.com/{parts[0]}/album/{slug}/{album_id}"
    return name, address


def _apple_song(url: str) -> str | None:
    """Apple's share button puts one song on the album address, as ?i=."""
    parsed = urlparse(url)
    if _site(url) != "music.apple.com":
        return None
    song_id = _query_value(parsed.query, "i")
    parts = [part for part in parsed.path.split("/") if part]
    if not song_id.isdigit() or len(parts) < 2 or len(parts[0]) != 2 or parts[1].lower() != "album":
        return None
    return f"https://music.apple.com/{parts[0]}/song/{song_id}"


def _preview(track: object, search: bool) -> bool:
    if getattr(track, "is_preview", None) is True:
        return True
    if not search:
        return False
    length = getattr(track, "length", None)
    if isinstance(length, bool) or not isinstance(length, int):
        return False
    return 0 < length <= PREVIEW_MS
