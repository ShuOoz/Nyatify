"""Turn a request into an Apple Music, YouTube, Spotify, SoundCloud, or NetEase track."""

from __future__ import annotations

import ipaddress
import logging
import weakref
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse, urlunparse

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
_REDIRECT_LIMIT = 5
_YOUTUBE = {host for host, (platform, _) in _SITES.items() if platform == "youtube"}
_SPOTIFY = {"track", "album", "playlist", "episode", "artist"}
_TRACKING = {"si", "feature", "pp", "fbclid"}

# A typed song name starts on Apple Music. The card can switch. A link keeps its own site.
NAME_SEARCH_PLATFORM = "apple"
MATCHES = 25

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
    # Apple puts the title in the path. The word after the country is the kind.
    if host == "music.apple.com":
        return _apple_mode(query)
    sections = _sections(query)
    # A SoundCloud title is a free slug. Only a real sets segment is a collection.
    if host == "soundcloud.com":
        return "playlist" if "sets" in sections else "track"
    if "playlist" in sections or "album" in sections:
        return "playlist"
    if "artist" in sections:
        return "artist"
    return "track"


def _apple_kind(query: str) -> str:
    parts = [part for part in urlparse(query).path.split("/") if part]
    if parts and len(parts[0]) == 2 and parts[0].isalpha():
        parts = parts[1:]
    return parts[0].lower() if parts else ""


def _apple_mode(query: str) -> str:
    if _apple_song(query):
        return "track"
    kind = _apple_kind(query)
    if kind in {"album", "playlist"}:
        return "playlist"
    if kind == "artist":
        return "artist"
    return "track"


def import_link(query: str) -> str | None:
    """A playlist or album Create can save. A watch link saves its list, when it has one."""
    if request_mode(query) == "playlist":
        return query
    return _youtube_list(query)


def same_url(left: str, right: str) -> bool:
    """True when both addresses are one recording."""
    return canonical_url(left) == canonical_url(right)


def different_track(left: str, right: str) -> bool:
    """True when both addresses are different recordings on one music site.

    A link from another site does not decide. A mirror can still match by its title.
    """
    if _host(urlparse(left)) not in _SITES or _host(urlparse(right)) not in _SITES:
        return False
    if platform_key(left) != platform_key(right):
        return False
    return not same_url(left, right)


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
    routed = _netease_route(parsed)
    base = url if routed == parsed else urlunparse(routed)
    parsed = routed
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    kept = [(key, value) for key, value in pairs if not _tracking(key, _host(parsed))]
    path = parsed.path.rstrip("/") if len(parsed.path) > 1 else parsed.path
    if kept == pairs and path == parsed.path and not parsed.fragment:
        return base
    return urlunparse(parsed._replace(path=path, query=urlencode(kept), fragment=""))


def _spotify_app_link(text: str) -> str | None:
    """spotify:track:id from the app, as the https page Lavalink opens."""
    if not text.lower().startswith("spotify:"):
        return None
    parts = text.split(":")
    if len(parts) < 3:
        return None
    kind, ident = parts[1].lower(), parts[2].split("?", 1)[0].strip()
    if kind not in _SPOTIFY or not ident or ident.lower() == "local":
        return None
    return f"https://open.spotify.com/{kind}/{ident}"


def _apple_path(url: str) -> tuple[str, list[str]] | None:
    """Country, then the path after it. A link that left the country off uses us."""
    if _site(url) != "music.apple.com":
        return None
    parts = [unquote(part) for part in urlparse(url).path.split("/") if part]
    if parts and len(parts[0]) == 2 and parts[0].isalpha():
        return parts[0].lower(), parts[1:]
    return "us", parts


def track_query(text: str) -> str | None:
    """A song name or a supported link. None is some other site. An empty string is blank."""
    query = " ".join(text.split())
    if not query:
        return ""
    app_link = _spotify_app_link(query)
    if app_link:
        query = app_link
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
    routed = _netease_route(parsed)
    if routed is not parsed:
        query = urlunparse(routed)
    return _spotify_url(query) or _apple_song(query) or query


def needs_redirect(query: str) -> bool:
    return _host(urlparse(query)) in _SHORT


def _blocked_address(host: str) -> bool:
    name = host.strip("[]")
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def _allowed_fetch(url: str) -> bool:
    """A short link may only be followed onto a music site this bot already plays."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    host = _host(parsed)
    if not host or _blocked_address(host):
        return False
    return host in _SHORT or host in _SITES


async def follow_redirect(url: str) -> str | None:
    try:
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            current = url
            final = None
            for _ in range(_REDIRECT_LIMIT):
                if not _allowed_fetch(current):
                    return None
                async with session.get(current, allow_redirects=False) as response:
                    location = response.headers.get("Location")
                    if response.status in {301, 302, 303, 307, 308} and location:
                        current = urljoin(current, location)
                        continue
                    final = str(response.url)
                    break
            else:
                return None
    except Exception:
        log.warning("music link did not open", exc_info=True)
        return None
    if not final:
        return None
    opened = apple_share(final) or track_query(final)
    if not opened or needs_redirect(opened):
        return None
    return opened


def drop_previews(tracks: list) -> list:
    """A clip SoundCloud marked as a preview is left out. A short song stays."""
    return [track for track in tracks if not _preview(track)]


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


def _netease_route(parsed):
    """NetEase keeps the song id after #. Saving that address has to keep the id."""
    host = _host(parsed)
    if host not in {"music.163.com", "y.music.163.com"}:
        return parsed
    fragment = parsed.fragment or ""
    if not fragment.startswith("/") or fragment.startswith("//"):
        return parsed
    path, _, query = fragment.partition("?")
    return parsed._replace(netloc="music.163.com", path=path, query=query, fragment="")


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


def _tracking(key: str, host: str = "") -> bool:
    name = key.lower()
    if name in _TRACKING or name.startswith("utm_"):
        return True
    # SoundCloud's ?in= is the share page. NetEase's userid is the sharer.
    if name == "in" and (host == "soundcloud.com" or host.endswith(".soundcloud.com")):
        return True
    if name == "userid" and host in {"music.163.com", "y.music.163.com"}:
        return True
    return False


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
    A title can be a number, as in 1989. A link with only the id has no name.
    """
    located = _apple_path(url)
    if located is None:
        return None
    country, parts = located
    if not parts or parts[0].lower() != "album":
        return None
    tail = parts[1:]
    if len(tail) >= 2 and tail[0] and tail[1]:
        slug, album_id = tail[0], tail[1]
        name = " ".join(word.capitalize() for word in slug.replace("-", " ").split())
        address = f"https://music.apple.com/{country}/album/{slug}/{album_id}"
        return name, address
    if len(tail) == 1 and tail[0]:
        return "", f"https://music.apple.com/{country}/album/{tail[0]}"
    return None


def apple_share(url: str) -> str | None:
    """The album address with ?i= kept, so a saved song can still open its album."""
    album = apple_album(url)
    if album is None:
        return None
    song_id = _query_value(urlparse(url).query, "i")
    if not song_id.isdigit():
        return None
    return f"{album[1]}?i={song_id}"


def kept_url(url: str) -> str:
    """The address to store. An Apple share keeps its album. Anything else is one recording."""
    return apple_share(url) or canonical_url(url)


# The album address that belonged to a track before its link was rewritten to /song/{id}.
_shares: weakref.WeakKeyDictionary[object, str] = weakref.WeakKeyDictionary()


def remember_share(track: object, url: str) -> None:
    """Keep the album address on this track. The search itself still uses the song link."""
    share = apple_share(url)
    if not share:
        return
    try:
        _shares[track] = share
    except TypeError:
        return


def remembered_share(track: object) -> str | None:
    """The album address kept for this track, or the one still on its link."""
    try:
        found = _shares.get(track)
    except TypeError:
        found = None
    if isinstance(found, str):
        return found
    uri = getattr(track, "uri", None)
    if isinstance(uri, str):
        return apple_share(uri)
    return None


def _apple_song(url: str) -> str | None:
    """Apple's share button puts one song on the album address, as ?i=."""
    song_id = _query_value(urlparse(url).query, "i")
    located = _apple_path(url)
    if located is None or not song_id.isdigit():
        return None
    country, parts = located
    if not parts or parts[0].lower() != "album":
        return None
    return f"https://music.apple.com/{country}/song/{song_id}"


def _preview(track: object) -> bool:
    """SoundCloud sets this on a clip. A real song can be thirty seconds."""
    return getattr(track, "is_preview", None) is True
