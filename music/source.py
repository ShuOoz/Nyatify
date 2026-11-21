"""Turn a request into an Apple Music, YouTube, Spotify, SoundCloud, or NetEase track."""

from __future__ import annotations

import base64
import ipaddress
import logging
import re
import weakref
from html import unescape
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

# Desktop host is what Lavalink opens. None keeps the host. Short hosts only redirect.
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
_SPOTIFY = {"track", "album", "playlist", "episode", "artist"}
_NETEASE = {host for host, (platform, _desktop) in _SITES.items() if platform == "netease"}
_TRACKING = {"si", "feature", "pp", "fbclid"}
# userid is who shared it. uct and uct2 are that share. The rest is the app build.
_NETEASE_SHARE = {"userid", "uct", "uct2", "app_version", "dlt"}
_REDIRECTS = {301, 302, 303, 307, 308}
_REDIRECT_LIMIT = 5
_OEMBED = {
    "spotify": "https://open.spotify.com/oembed",
    "youtube": "https://www.youtube.com/oembed",
    "soundcloud": "https://soundcloud.com/oembed",
}
NAME_SEARCH_PLATFORM = "apple"
MATCHES = 25
GENRES = (
    ("Jazz", "https://www.youtube.com/playlist?list=RDCLAK5uy_kQmVjdr90p2onIIAnijgU8u9iUUAOXM2I"),
    ("Classical", "https://www.youtube.com/playlist?list=RDCLAK5uy_mnJwDlxR05gYNAwsDSZYQNmI1eMm5qCYQ"),
    ("Lo-fi", "https://www.youtube.com/playlist?list=RDCLAK5uy_kb7EBi6y3GrtJri4_ZH56Ms786DFEimbM"),
    ("Rock", "https://www.youtube.com/playlist?list=RDCLAK5uy_nCM1qVE5wxDwTsQFaLSReFt1fNEeHdvNE"),
    ("Pop", "https://www.youtube.com/playlist?list=RDCLAK5uy_nOwL35BM_GUTEbdbw_9FmvQhPWWdd3sAg"),
    ("Electronic", "https://www.youtube.com/playlist?list=RDCLAK5uy_kBbljBMZ2exMXUc3MZdtXHsMwvGqf3eE8"),
    ("Hip hop", "https://www.youtube.com/playlist?list=RDCLAK5uy_lBGRuQnsG37Akr1CY4SxL0VWFbPrbO4gs"),
    ("R&B", "https://www.youtube.com/playlist?list=RDCLAK5uy_nGZRi-an-ruqiZlNJSGhCDHucdp2FBNfI"),
    ("Ambient", "https://www.youtube.com/playlist?list=RDCLAK5uy_lX29m3FBic7gGpiZ_4hhJWAzLu4Sz6OME"),
)
# Ordinary uploads. A livestream dies after a few seconds.
_STATIONS = (
    ("Lo-fi", "lofi", ("DfpdnFjfRvo", "eTP5PZ8NoeU", "FuCrTDiYRj0", "8b3fqIBrNW0", "iJm5cdWbh84")),
    ("Jazz", "jazz", ("lj4VncsqFQw", "4ZUhtr_3o7c", "BywDOO99Ia0", "x0cEyeO5V6s", "uHo0CaawTqs")),
    ("Classical", "classical", ("WLWJy1eXX2c", "E2LM3ZlcDnk", "shoVsQhou-8", "BI8Y44J6MgI", "nBPnvGLD6BA")),
    ("Ambient", "ambient", ("yUXwdnOJ_z0", "LA_JmZTuQLo", "6zxNSmCwVlk", "2iEBB0cGrFY", "tLeoq4hhI00")),
    ("Electronic", "electronic", ("F7wlQMx37yI", "a47T2qylqWU", "QM7L27N8-rE", "IJflpkRk_eU", "byGPEiHbsnc")),
)
RADIOS = tuple((label, key) for label, key, _videos in _STATIONS)
_APPLE_KINDS = {"song", "album", "playlist", "artist"}
_YOUTUBE_PATHS = {"shorts", "embed", "live", "v"}
_BLOCKED = ("is_private", "is_loopback", "is_link_local", "is_reserved", "is_multicast", "is_unspecified")

# Spotify writes the name and the address in either order. The opening quote closes the value.
_META = (
    ("(?:name|property)=([\"']){name}\\1[^>]*?content=([\"'])(.*?)\\2", 3),
    ("content=([\"'])(.*?)\\1[^>]*?(?:name|property)=([\"']){name}\\3", 2),
)
# 22 characters. A longer run would swallow the bytes that follow the id.
_ALBUM_IN_TRACK = re.compile(
    rb"(?:https://open\.spotify\.com/(?:intl-[A-Za-z0-9-]+/)?album/|spotify:album:)([A-Za-z0-9]{22})",
)
_releases: dict[str, tuple[str, str]] = {}
_shares: weakref.WeakKeyDictionary[object, str] = weakref.WeakKeyDictionary()


def _open(url):
    parsed = urlparse(url) if isinstance(url, str) else url
    host = (parsed.hostname or "").lower().removeprefix("www.")
    parts = [piece for piece in (parsed.path or "").split("/") if piece]
    row = _SITES.get(host)
    platform = row[0] if row else ""
    desktop = (row[1] or host) if row else host
    return parsed, host, parts, platform, desktop

def _query_value(query: str, key: str) -> str:
    return next((value.strip() for name, value in parse_qsl(query) if name == key and value.strip()), "")

def _client():
    import aiohttp
    return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8))

def _text(value: object) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""

def _read(values, parse):
    for value in values:
        got = parse(value)
        if got:
            return got
    return None

def _slug_words(slug: str) -> str:
    if not slug or slug.isdigit() or not any(character.isalpha() for character in slug):
        return ""
    return " ".join(slug.replace("-", " ").replace("_", " ").split())

def _apple(url: str) -> tuple[str, str, list[str], str] | None:
    parsed, _host, parts, _platform, desktop = _open(url)
    if desktop != "music.apple.com":
        return None
    parts = [unquote(part) for part in parts]
    if parts and len(parts[0]) == 2 and parts[0].isalpha():
        country, parts = parts[0].lower(), parts[1:]
    else:
        country = "us"
    kind = parts[0].lower() if parts else ""
    # Apple's share button puts one song on the album address, as ?i=.
    return country, kind, parts[1:], _query_value(parsed.query, "i")

def _apple_song(url: str) -> str | None:
    found = _apple(url)
    if found is None or found[1] != "album" or not found[3].isdigit():
        return None
    return f"https://music.apple.com/{found[0]}/song/{found[3]}"

def _youtube(url: str) -> tuple[str, str]:
    parsed, host, parts, platform, _desktop = _open(url)
    if platform != "youtube":
        return "", ""
    if host == "youtu.be":
        video = parts[0] if parts else ""
    elif parts == ["watch"]:
        video = _query_value(parsed.query, "v")
    elif len(parts) > 1 and parts[0] in _YOUTUBE_PATHS:
        video = parts[1]
    else:
        video = ""
    return video, _query_value(parsed.query, "list")

def _spotify_https(kind: str, ident: str, *, reject_local: bool = False) -> str | None:
    ident = ident.split("?", 1)[0].strip()
    if kind not in _SPOTIFY or not ident or (reject_local and ident.lower() == "local"):
        return None
    return f"https://open.spotify.com/{kind}/{ident}"

def _spotify_app_link(text: str) -> str | None:
    # spotify:track:id from the app, as the https page Lavalink opens.
    if not text.lower().startswith("spotify:"):
        return None
    parts = text.split(":")
    if len(parts) < 3:
        return None
    return _spotify_https(parts[1].lower(), parts[2], reject_local=True)

def _spotify_url(url: str) -> str | None:
    _parsed, host, parts, _platform, _desktop = _open(url)
    if host != "open.spotify.com":
        return None
    while parts and (parts[0] == "embed" or parts[0].startswith("intl-")):
        parts = parts[1:]
    if len(parts) < 2:
        return None
    return _spotify_https(parts[0], parts[1])

def _netease_route(parsed):
    # NetEase keeps the song id after #.
    if _open(parsed)[3] != "netease":
        return parsed
    fragment = parsed.fragment or ""
    if not fragment.startswith("/") or fragment.startswith("//"):
        return parsed
    path, _, query = fragment.partition("?")
    return parsed._replace(netloc="music.163.com", path=path, query=query, fragment="")

def _sections(parsed) -> set[str]:
    text = f"{parsed.path or ''}/{parsed.fragment or ''}"
    heads = (part.split("?", 1)[0].split("&", 1)[0].strip().lower() for part in text.split("/"))
    return {head for head in heads if head}

def _tracking(key: str, host: str = "") -> bool:
    name = key.lower()
    if name in _TRACKING or name.startswith("utm_"):
        return True
    # SoundCloud's ?in= is the share page.
    if name == "in" and (host == "soundcloud.com" or host.endswith(".soundcloud.com")):
        return True
    return name in _NETEASE_SHARE and host in _NETEASE

def station_videos(key: str) -> tuple[str, ...]:
    videos = next((ids for _label, station, ids in _STATIONS if station == key), ())
    return tuple(f"https://www.youtube.com/watch?v={video}" for video in videos)

def normalize_platform(name: str | None) -> str:
    key = (name or "").strip().lower()
    return key if key in _PLATFORMS else "youtube"

def platform_key(query: str, platform: str = "youtube") -> str:
    return _open(query)[3] or normalize_platform(platform)

def platform_label(query: str, platform: str = "youtube") -> str:
    return _PLATFORMS[platform_key(query, platform)][0]

def platform_pairs() -> tuple[tuple[str, str], ...]:
    return tuple((key, label) for key, (label, _prefix) in _PLATFORMS.items())

def platform_line(*, last: str = "and") -> str:
    labels = [label for label, _prefix in _PLATFORMS.values()]
    return f"{', '.join(labels[:-1])}, {last} {labels[-1]}"

def is_link(query: str) -> bool:
    parsed = _open(query)[0]
    return bool(parsed.scheme and parsed.netloc)

def link_words(url: str) -> str:
    text = " ".join(url.split())
    target = text if "://" in text else f"https://{text}"
    found = _apple(target)
    if found is not None:
        _country, kind, tail, _song = found
        return _slug_words(tail[0]) if tail and kind in _APPLE_KINDS else ""
    _parsed, _host, parts, _platform, desktop = _open(target)
    parts = [unquote(part) for part in parts if unquote(part).lower() != "sets"]
    if desktop != "soundcloud.com" or len(parts) < 2:
        return ""
    title, artist = _slug_words(parts[-1]), _slug_words(parts[0])
    if title and artist and artist.casefold() not in title.casefold():
        return f"{title} {artist}"
    return title

def request_mode(query: str) -> str:
    parsed, _host, _parts, _platform, desktop = _open(query)
    if not parsed.scheme or not parsed.netloc:
        return "name"
    if desktop == "music.apple.com":
        _country, kind, _tail, song = _apple(query)
        if kind == "album" and song.isdigit():
            return "track"
        if kind in {"album", "playlist"}:
            return "playlist"
        return "artist" if kind == "artist" else "track"
    # A SoundCloud title is a free slug. Only a real sets segment is a collection.
    sections = _sections(parsed)
    if desktop == "soundcloud.com":
        return "playlist" if "sets" in sections else "track"
    if sections & {"playlist", "album"}:
        return "playlist"
    return "artist" if "artist" in sections else "track"

def import_link(query: str) -> str | None:
    if request_mode(query) == "playlist":
        return query
    _video, list_id = _youtube(query)
    return f"https://www.youtube.com/playlist?list={list_id}" if list_id else None

def same_url(left: str, right: str) -> bool:
    return canonical_url(left) == canonical_url(right)

def different_track(left: str, right: str) -> bool:
    one, two = _open(left)[3], _open(right)[3]
    return bool(one and one == two and not same_url(left, right))

def canonical_url(url: str) -> str:
    video, _list_id = _youtube(url)
    direct = f"https://www.youtube.com/watch?v={video}" if video else _spotify_url(url) or _apple_song(url)
    if direct:
        return direct
    parsed = _open(url)[0]
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return url
    routed = _netease_route(parsed)
    host = _open(routed)[1]
    pairs = parse_qsl(routed.query, keep_blank_values=True)
    kept = [item for item in pairs if not _tracking(item[0], host)]
    # Lavalink reads song?id=. A share token in front of id hides the song.
    if host in _NETEASE and kept and kept[0][0] != "id":
        kept = [item for item in kept if item[0] == "id"] + [item for item in kept if item[0] != "id"]
    path = routed.path.rstrip("/") if len(routed.path) > 1 else routed.path
    if kept == pairs and path == routed.path and not routed.fragment:
        return url if routed == parsed else urlunparse(routed)
    return urlunparse(routed._replace(path=path, query=urlencode(kept), fragment=""))

def track_query(text: str) -> str | None:
    query = " ".join(text.split())
    if not query:
        return ""
    query = _spotify_app_link(query) or query
    parsed, host, _parts, platform, desktop = _open(query)
    if not parsed.scheme and not parsed.netloc:
        bare = query.split("/", 1)[0].split("?", 1)[0].lower().removeprefix("www.")
        if bare not in _SITES:
            return query
        query = f"https://{query}"
        parsed, host, _parts, platform, desktop = _open(query)
    if not platform:
        return None
    if desktop != host:
        parsed = parsed._replace(netloc=desktop)
        query = parsed.geturl()
    routed = _netease_route(parsed)
    if routed is not parsed:
        query = urlunparse(routed)
    query = _spotify_url(query) or _apple_song(query) or query
    return canonical_url(query) if _open(query)[3] == "netease" else query

def needs_redirect(query: str) -> bool:
    return _open(query)[1] in _SHORT

def _allowed_fetch(url: str) -> bool:
    parsed, host, _parts, platform, _desktop = _open(url)
    if parsed.scheme not in ("http", "https") or not host:
        return False
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        address = None
    if address is not None and any(getattr(address, name) for name in _BLOCKED):
        return False
    return bool(platform)

async def follow_redirect(url: str) -> str | None:
    current = url
    try:
        async with _client() as session:
            for _ in range(_REDIRECT_LIMIT):
                if not _allowed_fetch(current):
                    return None
                async with session.get(current, allow_redirects=False) as response:
                    location = response.headers.get("Location")
                    if response.status in _REDIRECTS and location:
                        current = urljoin(current, location)
                        continue
                    current = str(response.url)
                    break
            else:
                return None
    except Exception:
        log.warning("music link did not open", exc_info=True)
        return None
    opened = apple_share(current) or track_query(current)
    return opened if opened and not needs_redirect(opened) else None

async def heard_title(url: str) -> str:
    query = track_query(url) or ""
    named = link_words(url) or (link_words(query) if query and query != url else "")
    if named or not is_link(query):
        return named
    endpoint = _OEMBED.get(platform_key(query))
    body = await _oembed(endpoint, query) if endpoint else {}
    title, author = _text(body.get("title")), _text(body.get("author_name"))
    if platform_key(query) == "spotify" and title and author and author.casefold() not in title.casefold():
        return f"{title} {author}"
    return title

async def _oembed(endpoint: str, url: str) -> dict:
    try:
        async with _client() as session:
            async with session.get(endpoint, params={"url": url, "format": "json"}) as response:
                if response.status != 200:
                    return {}
                body = await response.json(content_type=None)
                return body if isinstance(body, dict) else {}
    except Exception:
        log.warning("music link had no title", exc_info=True)
        return {}

def unplayable(track: object) -> bool:
    # Only NetEase uses the preview flag for a song that must stay off the queue.
    if str(getattr(track, "source", "") or "").casefold() != "netease":
        return False
    return getattr(track, "is_preview", None) is True

def drop_previews(tracks: list) -> list:
    return [track for track in tracks if getattr(track, "is_preview", None) is not True]

def take_tracks(results: object, mode: str) -> list:
    raw = getattr(results, "tracks", None)
    found = list(raw if isinstance(raw, list) else results if isinstance(results, list) else [])
    if mode in ("playlist", "draw"):
        return found
    if mode != "track":
        return found[:MATCHES]
    index = getattr(results, "selected", None)
    if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(found):
        return [found[index]]
    return found[:1]

async def search_track(query: str, platform: str = "youtube", *, node: object | None = None):
    extra = {} if node is None else {"node": node}
    if is_link(query):
        return await wavelink.Playable.search(query, **extra)
    _label, prefix = _PLATFORMS[normalize_platform(platform)]
    return await wavelink.Playable.search(query, source=prefix, **extra)

def _meta(html: str, key: str) -> str | None:
    name = re.escape(key)
    for pattern, group in _META:
        found = re.search(rf"<meta\s[^>]*?{pattern.format(name=name)}", html, re.IGNORECASE)
        if found:
            return unescape(found.group(group)).strip()
    return None

def spotify_album(url: str) -> str | None:
    text = " ".join(url.split())
    if text.lower().startswith("spotify:album:"):
        page = _spotify_https("album", text.split(":", 2)[-1], reject_local=True)
    else:
        page = _spotify_url(text)
    return page if page and "/album/" in page else None

def page_album(html: str) -> tuple[str, str] | None:
    # og:description is "Artist · Album · Song · year".
    page = spotify_album(_meta(html, "music:album") or "")
    if page is None:
        return None
    parts = [part.strip() for part in (_meta(html, "og:description") or "").split("·")]
    return (parts[1] if len(parts) >= 2 else ""), page

async def _head(content, limit: int = 256 * 1024) -> bytes:
    # One read is the buffered packet, which ends before Spotify's album tag.
    parts: list[bytes] = []
    size = 0
    while size < limit:
        block = await content.read(limit - size)
        if not block:
            break
        parts.append(block)
        size += len(block)
        if b"</head>" in b"".join(parts).lower():
            break
    return b"".join(parts)

async def spotify_release(url: str) -> tuple[str, str] | None:
    query = track_query(url)
    if not query or platform_key(query) != "spotify" or "/track/" not in query:
        return None
    if query in _releases:
        return _releases[query]
    try:
        async with _client() as session:
            async with session.get(query, headers={"User-Agent": "Mozilla/5.0"}) as response:
                raw = await _head(response.content) if response.status == 200 else b""
    except Exception:
        log.warning("music could not read the spotify album", exc_info=True)
        return None
    found = page_album(raw.decode("utf-8", "replace")) if raw else None
    if found is not None:
        _releases[query] = found
    return found

def apple_album(url: str) -> tuple[str, str] | None:
    found = _apple(url)
    if found is None or found[1] != "album" or not found[2] or not found[2][0]:
        return None
    country, tail = found[0], found[2]
    page = f"https://music.apple.com/{country}/album/{tail[0]}"
    if len(tail) == 1:
        return "", page
    if not tail[1]:
        return None
    name = " ".join(word.capitalize() for word in tail[0].replace("-", " ").split())
    return name, f"{page}/{tail[1]}"

def apple_share(url: str) -> str | None:
    found = _apple(url)
    if found is None or not found[3].isdigit():
        return None
    album = apple_album(url)
    return None if album is None else f"{album[1]}?i={found[3]}"

def kept_url(url: str) -> str:
    return apple_share(url) or canonical_url(url)

def remember_share(track: object, url: str) -> None:
    share = apple_share(url)
    if not share:
        return
    try:
        _shares[track] = share
    except TypeError:
        return

def remembered_share(track: object) -> str | None:
    try:
        found = _shares.get(track)
    except TypeError:
        found = None
    if isinstance(found, str):
        return found
    uri = getattr(track, "uri", None)
    return apple_share(uri) if isinstance(uri, str) else None

def _album_page(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    page = spotify_album(value)
    if page:
        return page
    if not value.startswith(("http://", "https://")) or " " in value:
        return None
    query = track_query(value)
    return query if query and request_mode(query) == "playlist" else None

def _plugin(track: object) -> dict:
    raw = getattr(track, "raw_data", None)
    info = raw.get("pluginInfo") if isinstance(raw, dict) else None
    return info if isinstance(info, dict) else {}

def _encoded_album(track: object) -> str | None:
    encoded = getattr(track, "encoded", None)
    if not isinstance(encoded, str) or len(encoded) < 16:
        return None
    try:
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), validate=False)
    except Exception:
        return None
    found = _ALBUM_IN_TRACK.search(raw)
    return None if found is None else f"https://open.spotify.com/album/{found.group(1).decode()}"

def _album(track: object) -> tuple[str, str | None] | None:
    plugin = _plugin(track)
    nested = plugin.get("album")
    album = getattr(track, "album", None)
    names = [getattr(album, "name", None), plugin.get("albumName")]
    pages = [getattr(album, "url", None), plugin.get("albumUrl"), plugin.get("albumURI")]
    if isinstance(nested, dict):
        links = nested.get("external_urls")
        names.append(nested.get("name"))
        pages.extend((nested.get("url"), links.get("spotify") if isinstance(links, dict) else None))
    elif isinstance(nested, str):
        names.append(nested)
    playlist = getattr(track, "playlist", None)
    if str(getattr(playlist, "type", "") or "").casefold() == "album":
        names.append(getattr(playlist, "name", None))
        pages.append(getattr(playlist, "url", None))
    name = _read(names, _text) or ""
    url = _read(pages, _album_page)
    share = remembered_share(track) or getattr(track, "uri", None)
    linked = apple_album(share) if isinstance(share, str) else None
    if linked:
        name = name or linked[0]
        if url is None or "?i=" in url:
            url = linked[1]
    url = url or _encoded_album(track)
    if not name and url is None:
        return None
    return name or "Album", url

def _spotify_track(track: object) -> str | None:
    for value in (getattr(track, "uri", None), _plugin(track).get("save_uri")):
        query = track_query(value) if isinstance(value, str) else None
        if query and platform_key(query) == "spotify" and "/track/" in query:
            return query
    ident = getattr(track, "identifier", None)
    if getattr(track, "source", None) == "spotify" and isinstance(ident, str) and ident and ident != "local":
        return f"https://open.spotify.com/track/{ident}"
    return None
