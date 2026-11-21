"""Turn a request into a track. No Discord login and no Lavalink node."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import wavelink

from music.lavalink import Found, find_songs
from music.source import (
    GENRES,
    MATCHES,
    _allowed_fetch,
    apple_album,
    page_album,
    spotify_album,
    canonical_url,
    follow_redirect,
    heard_title,
    import_link,
    link_words,
    needs_redirect,
    normalize_platform,
    platform_key,
    request_mode,
    search_track,
    take_tracks,
    track_query,
)

def test_a_link_keeps_its_platform():
    """Platform names and a link's shape stay what was pasted."""
    case_platform_names()
    case_a_link_keeps_its_shape()


async def test_a_short_packet_still_has_the_album(monkeypatch):
    """The first packet ends before the album tag. The song still has an album."""
    from music import source

    url = "https://open.spotify.com/track/2EqlS6tkEnglzr7tkKAAYD"
    album = "https://open.spotify.com/album/0ETFjACtuP2ADo6LFhL6HN"
    first = (
        '<html><meta property="og:description" content="The Beatles · Abbey Road · Song · 1969"/>'.encode()
        + b" " * 14000
    )
    second = f'<meta name="music:album" content="{album}"/></head>'.encode()
    decoy = b'<meta name="music:album" content="https://open.spotify.com/album/WRONGWRONGWRONGWRONGW"/>'
    incoming = [first, second, decoy]
    reads = 0

    class Content:
        async def read(self, _n):
            nonlocal reads
            reads += 1
            if not incoming:
                return b""
            return incoming.pop(0)

    class Response:
        status = 200
        content = Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class Session:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def get(self, query, headers=None):
            assert query == url
            return Response()

    monkeypatch.setattr("aiohttp.ClientSession", Session)
    source._releases.pop(url, None)
    try:
        assert await source.spotify_release("https://open.spotify.com/album/abc") is None
        assert await source.spotify_release("https://www.youtube.com/watch?v=abc") is None
        assert await source.spotify_release(url) == ("Abbey Road", album)
        assert reads == 2
        assert incoming == [decoy]
        assert await source.spotify_release(url) == ("Abbey Road", album)
        assert reads == 2
    finally:
        source._releases.pop(url, None)

def case_platform_names():
    assert normalize_platform(None) == "youtube"
    assert normalize_platform("Spotify") == "spotify"
    assert normalize_platform("apple") == "apple"
    assert normalize_platform("SoundCloud") == "soundcloud"
    assert normalize_platform("NetEase") == "netease"
    assert normalize_platform("deezer") == "youtube"
    assert platform_key("https://geo.music.apple.com/us/album/x/1") == "apple"
    assert platform_key("https://y.music.163.com/m/song?id=1") == "netease"
    assert platform_key("https://m.soundcloud.com/rick/never") == "soundcloud"
    assert platform_key("https://www.m.soundcloud.com/rick/never") == "soundcloud"

def case_a_link_keeps_its_shape():
    assert request_mode("one wish") == "name"
    assert request_mode("https://www.youtube.com/watch?v=abc") == "track"
    assert request_mode("https://www.youtube.com/watch?v=abc&list=PL1") == "track"
    assert request_mode("https://youtu.be/abc") == "track"
    assert request_mode("https://www.youtube.com/playlist?list=PL1") == "playlist"
    assert request_mode("https://open.spotify.com/playlist/abc") == "playlist"
    assert request_mode("https://music.apple.com/us/playlist/evening/pl.1") == "playlist"
    assert request_mode("https://soundcloud.com/user/sets/evening") == "playlist"
    assert request_mode("https://music.163.com/playlist?id=1") == "playlist"
    assert request_mode("https://music.163.com/#/playlist?id=1") == "playlist"
    assert request_mode("https://music.163.com/song?id=1") == "track"
    assert request_mode("https://open.spotify.com/album/abc") == "playlist"
    assert request_mode("https://music.apple.com/us/album/name/1") == "playlist"
    assert request_mode("https://music.163.com/album?id=1") == "playlist"
    assert request_mode("https://open.spotify.com/artist/abc") == "artist"
    assert request_mode("https://music.apple.com/us/artist/name/1") == "artist"
    assert request_mode("https://music.apple.com/us/album/animals/1533388849?i=1533388859") == "track"
    assert request_mode("https://music.apple.com/album/folklore/1528112358?i=1528112359") == "track"
    assert apple_album("https://music.apple.com/us/album/chet-baker-sings/1440812340?i=1440811111") == (
        "Chet Baker Sings",
        "https://music.apple.com/us/album/chet-baker-sings/1440812340",
    )
    assert apple_album("https://music.apple.com/us/song/its-always-you/1") is None
    assert apple_album("https://music.apple.com/us/album/1989/907242701?i=907242702") == (
        "1989",
        "https://music.apple.com/us/album/1989/907242701",
    )
    assert apple_album("https://music.apple.com/us/album/21/1544491602") == (
        "21",
        "https://music.apple.com/us/album/21/1544491602",
    )
    assert apple_album("https://music.apple.com/us/album/1440812340?i=1440812692") == (
        "",
        "https://music.apple.com/us/album/1440812340",
    )
    assert spotify_album("spotify:album:abc") == "https://open.spotify.com/album/abc"
    assert spotify_album("https://open.spotify.com/intl-de/album/abc?si=1") == (
        "https://open.spotify.com/album/abc"
    )
    assert spotify_album("https://open.spotify.com/track/abc") is None
    assert spotify_album("https://i.scdn.co/image/ab") is None
    page = (
        '<meta property="og:description" content="The Beatles · Abbey Road &amp; More · Song · 1969"/>'
        '<meta name="music:album" content="https://open.spotify.com/album/0ETFjACtuP2ADo6LFhL6HN"/>'
    )
    assert page_album(page) == ("Abbey Road & More", "https://open.spotify.com/album/0ETFjACtuP2ADo6LFhL6HN")
    reversed_page = (
        '<meta content="https://open.spotify.com/intl-de/album/0ETFjACtuP2ADo6LFhL6HN?si=1" name="music:album"/>'
    )
    assert page_album(reversed_page) == ("", "https://open.spotify.com/album/0ETFjACtuP2ADo6LFhL6HN")
    assert page_album("<meta name='music:album' content='https://i.scdn.co/image/ab'/>") is None
    pepper = (
        '<meta name="music:album:track" content="https://open.spotify.com/album/WRONGWRONGWRONGWRONGW"/>'
        '<meta property="og:description" content="The Beatles · Sgt. Pepper\'s Lonely Hearts Club Band · Song · 1967"/>'
        '<meta name="music:album" content="https://open.spotify.com/album/6QaVfG1pHYl1z15ZxkvVDW"/>'
    )
    assert page_album(pepper) == (
        "Sgt. Pepper's Lonely Hearts Club Band",
        "https://open.spotify.com/album/6QaVfG1pHYl1z15ZxkvVDW",
    )
    assert import_link("https://music.apple.com/us/album/animals/1533388849?i=1533388859") is None
    assert request_mode("https://soundcloud.com/user/album-of-the-year") == "track"
    assert request_mode("https://soundcloud.com/user/artist-name") == "track"
    assert request_mode("https://soundcloud.com/user/playlist-night") == "track"
    assert request_mode("https://music.apple.com/us/song/artists-choice/1") == "track"
    assert request_mode("https://music.apple.com/us/song/album/123") == "track"
    assert request_mode("https://music.apple.com/us/song/playlist/123") == "track"
    assert request_mode("https://music.apple.com/us/song/artist/123") == "track"
    assert request_mode("https://music.apple.com/us/song/sets/123") == "track"
    assert request_mode("https://music.apple.com/us/artist/album/123") == "artist"
    assert request_mode("https://music.apple.com/us/artist/playlist/123") == "artist"
    assert request_mode("https://open.spotify.com/intl-de/album/abc") == "playlist"
    lists = dict(GENRES)
    assert lists["Jazz"] == "https://www.youtube.com/playlist?list=RDCLAK5uy_kQmVjdr90p2onIIAnijgU8u9iUUAOXM2I"
    assert lists["Rock"] == "https://www.youtube.com/playlist?list=RDCLAK5uy_nCM1qVE5wxDwTsQFaLSReFt1fNEeHdvNE"
    assert all(request_mode(url) == "playlist" for url in lists.values())
    assert all(url.startswith("https://www.youtube.com/playlist?list=") for url in lists.values())

def test_a_request_becomes_tracks():
    """A query keeps names and supported links. Taking tracks follows that shape."""
    case_track_query_keeps_names_and_supported_links()
    case_take_tracks_follows_the_link_shape()

def case_track_query_keeps_names_and_supported_links():
    assert track_query("  ocean   drive ") == "ocean drive"
    assert track_query("") == ""
    assert track_query("   ") == ""
    link = "https://www.youtube.com/watch?v=abc"
    assert track_query(link) == link
    assert track_query("https://youtu.be/abc") == "https://youtu.be/abc"
    assert track_query("https://music.youtube.com/watch?v=abc") == "https://music.youtube.com/watch?v=abc"
    spotify = "https://open.spotify.com/track/abc"
    assert track_query(spotify) == spotify
    apple = "https://music.apple.com/us/song/never-gonna-give-you-up/1559885421"
    assert track_query(apple) == apple
    soundcloud = "https://soundcloud.com/rick-astley-official/never-gonna-give-you-up"
    assert track_query(soundcloud) == soundcloud
    mobile = "https://m.soundcloud.com/rick-astley-official/never-gonna-give-you-up"
    assert track_query(mobile) == "https://soundcloud.com/rick-astley-official/never-gonna-give-you-up"
    assert track_query("https://on.soundcloud.com/abc") == "https://on.soundcloud.com/abc"
    assert track_query("https://spotify.link/abc") == "https://spotify.link/abc"
    assert needs_redirect("https://on.soundcloud.com/abc")
    assert needs_redirect("https://spotify.link/abc")
    assert import_link("https://open.spotify.com/album/abc") == "https://open.spotify.com/album/abc"
    assert import_link("https://music.apple.com/us/album/name/123") == (
        "https://music.apple.com/us/album/name/123"
    )
    assert import_link("https://www.youtube.com/watch?v=abc&list=PLxxxx") == (
        "https://www.youtube.com/playlist?list=PLxxxx"
    )
    assert import_link("https://music.youtube.com/watch?v=abc&list=PLxxxx") == (
        "https://www.youtube.com/playlist?list=PLxxxx"
    )
    assert import_link("https://www.youtube.com/watch?v=abc") is None
    assert import_link("https://open.spotify.com/playlist/abc") == "https://open.spotify.com/playlist/abc"
    assert track_query("https://geo.music.apple.com/us/album/x/1") == "https://music.apple.com/us/album/x/1"
    assert track_query("https://y.music.163.com/m/song?id=1") == "https://music.163.com/m/song?id=1"
    assert track_query("open.spotify.com/track/abc") == "https://open.spotify.com/track/abc"
    netease = "https://music.163.com/song?id=186016"
    assert track_query(netease) == netease
    assert track_query("https://music.163.com/#/song?id=186016") == "https://music.163.com/song?id=186016"
    assert track_query("https://y.music.163.com/#/playlist?id=1") == "https://music.163.com/playlist?id=1"
    assert canonical_url("https://music.163.com/#/song?id=186016") == "https://music.163.com/song?id=186016"
    assert canonical_url("https://music.163.com/song?id=186016&userid=1") == (
        "https://music.163.com/song?id=186016"
    )
    shared = (
        "https://music.163.com/#/song?uct2=U2FsdGVkX19YAoNdVGx0FIKolQxM1bhpsWnEtIzEeNU=&id=1341578257"
    )
    assert track_query(shared) == "https://music.163.com/song?id=1341578257"
    assert canonical_url(shared) == "https://music.163.com/song?id=1341578257"
    assert track_query(
        "https://y.music.163.com/m/song?app_version=8.8.45&id=95670&uct2=abc%3D%3D&dlt=0846"
    ) == "https://music.163.com/m/song?id=95670"
    assert canonical_url("https://soundcloud.com/artist/song?in=user/sets/night") == (
        "https://soundcloud.com/artist/song"
    )
    assert canonical_url("https://y.music.163.com/#/playlist?id=1") == "https://music.163.com/playlist?id=1"
    apple_song = "https://music.apple.com/us/album/animals/1533388849?i=1533388859"
    assert track_query(apple_song) == "https://music.apple.com/us/song/1533388859"
    assert track_query("https://music.apple.com/album/folklore/1528112358?i=1528112359") == (
        "https://music.apple.com/us/song/1528112359"
    )
    assert track_query("spotify:track:6habFhsOp2NvshLv26DqMb") == (
        "https://open.spotify.com/track/6habFhsOp2NvshLv26DqMb"
    )
    assert track_query("spotify:album:abc") == "https://open.spotify.com/album/abc"
    assert track_query("https://embed.music.apple.com/us/album/animals/1?i=2&uo=4") == (
        "https://music.apple.com/us/song/2"
    )
    assert link_words("https://music.apple.com/us/song/never-gonna-give-you-up/1") == (
        "never gonna give you up"
    )
    assert link_words("https://music.apple.com/us/song/1533388859") == ""
    assert link_words("https://soundcloud.com/rick/never-gonna") == "never gonna rick"
    assert link_words("https://open.spotify.com/track/abc") == ""
    assert track_query("https://open.spotify.com/embed/track/abc?si=1") == "https://open.spotify.com/track/abc"
    assert track_query("https://open.spotify.com/intl-de/track/abc") == "https://open.spotify.com/track/abc"
    assert canonical_url(apple_song + "&uo=4") == "https://music.apple.com/us/song/1533388859"
    from music.source import _album, apple_share, remember_share

    assert apple_share(apple_song + "&uo=4") == apple_song
    song_only = MagicMock()
    song_only.uri = "https://music.apple.com/us/song/1533388859"
    song_only.album = None
    song_only.raw_data = {}
    song_only.encoded = ""
    song_only.playlist = None
    remember_share(song_only, apple_song + "&uo=4")
    assert _album(song_only) == ("Animals", "https://music.apple.com/us/album/animals/1533388849")
    assert canonical_url("https://open.spotify.com/embed/intl-en/track/abc") == (
        "https://open.spotify.com/track/abc"
    )

class _Redirect:
    def __init__(self, hops):
        self.hops = list(hops)
        self.requested = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def get(self, url, allow_redirects=False):
        self.requested.append((url, allow_redirects))
        status, location, final = self.hops.pop(0)
        return _Page(status, final, location)


class _Page:
    def __init__(self, status, url, location):
        self.status = status
        self.url = url
        self.headers = {"Location": location} if location else {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


async def test_a_lookup_stays_on_the_chosen_site(monkeypatch):
    """A short link stops before a private address. SoundCloud search drops previews. Search uses the chosen platform."""
    with monkeypatch.context() as patch:
        await case_a_short_link_stops_before_a_private_address(patch)
    with monkeypatch.context() as patch:
        await case_soundcloud_search_drops_previews(patch)
    with monkeypatch.context() as patch:
        await case_search_uses_the_chosen_platform(patch)

async def case_a_short_link_stops_before_a_private_address(monkeypatch):
    import aiohttp

    blocked = _Redirect([(302, "http://127.0.0.1/secret", "https://spotify.link/abc")])
    monkeypatch.setattr(aiohttp, "ClientSession", lambda *args, **kwargs: blocked)
    assert await follow_redirect("https://spotify.link/abc") is None
    assert blocked.requested == [("https://spotify.link/abc", False)]
    assert _allowed_fetch("http://169.254.169.254/") is False
    assert _allowed_fetch("http://10.0.0.8/") is False
    assert _allowed_fetch("https://evil.example/track") is False

    opened = _Redirect([
        (302, "https://open.spotify.com/track/abc", "https://spotify.link/abc"),
        (200, None, "https://open.spotify.com/track/abc"),
    ])
    monkeypatch.setattr(aiohttp, "ClientSession", lambda *args, **kwargs: opened)
    assert await follow_redirect("https://spotify.link/abc") == "https://open.spotify.com/track/abc"
    assert [url for url, _followed in opened.requested] == [
        "https://spotify.link/abc",
        "https://open.spotify.com/track/abc",
    ]


async def case_soundcloud_search_drops_previews(monkeypatch):
    preview = MagicMock(length=30_000, is_preview=False, title="Billie Jean")
    marked = MagicMock(length=248_000, is_preview=True, title="Snippet")
    short = MagicMock(length=20_000, is_preview=False, title="Her Majesty")
    full = MagicMock(length=248_000, is_preview=False, title="Beat It")
    found = AsyncMock(return_value=[preview, marked, short, full])
    monkeypatch.setattr("music.lavalink.node_is_ready", lambda: True)
    monkeypatch.setattr("music.source.search_track", found)
    guild = MagicMock()

    songs = await find_songs(guild, "michael jackson", "soundcloud")
    assert isinstance(songs, Found)
    assert songs.tracks == [preview, short, full]

    youtube = await find_songs(guild, "michael jackson", "youtube")
    assert isinstance(youtube, Found)
    assert youtube.tracks == [preview, marked, short, full]

    found.return_value = [preview]
    direct = await find_songs(guild, "https://soundcloud.com/mj/billie-jean", "youtube")
    assert isinstance(direct, Found)
    assert direct.tracks == [preview]

    found.return_value = [marked]
    assert await find_songs(guild, "https://soundcloud.com/mj/snippet", "youtube") == (
        "Nothing on SoundCloud matched that."
    )

def case_take_tracks_follows_the_link_shape():
    songs = ["one", "two", "three", "four", "five", "six"]
    assert take_tracks(songs, "name") == songs
    assert take_tracks(["x"] * (MATCHES + 3), "name") == ["x"] * MATCHES
    assert take_tracks(songs, "track") == ["one"]
    assert take_tracks(["x"] * (MATCHES + 3), "artist") == ["x"] * MATCHES
    assert take_tracks(songs, "playlist") == songs
    playlist = type("Playlist", (), {"tracks": songs})()
    assert take_tracks(playlist, "playlist") == songs
    assert take_tracks(playlist, "name") == songs
    assert take_tracks(None, "name") == []
    marked = type("Playlist", (), {"tracks": songs, "selected": 2})()
    assert take_tracks(marked, "track") == ["three"]
    assert take_tracks(marked, "playlist") == songs
    assert take_tracks(type("Playlist", (), {"tracks": songs, "selected": -1})(), "track") == ["one"]

async def case_search_uses_the_chosen_platform(monkeypatch):
    called = {}

    async def search(query, *, source=None, node=None):
        called["query"] = query
        called["source"] = source
        if node is not None:
            called["node"] = node
        return []

    monkeypatch.setattr(wavelink.Playable, "search", search)
    assert await search_track("ocean drive") == []
    assert called == {"query": "ocean drive", "source": wavelink.TrackSource.YouTube}

    called.clear()
    assert await search_track("ocean drive", "spotify") == []
    assert called == {"query": "ocean drive", "source": "spsearch"}

    called.clear()
    assert await search_track("ocean drive", "apple") == []
    assert called == {"query": "ocean drive", "source": "amsearch"}

    called.clear()
    assert await search_track("ocean drive", "soundcloud") == []
    assert called == {"query": "ocean drive", "source": "scsearch"}

    called.clear()
    assert await search_track("晴天", "netease") == []
    assert called == {"query": "晴天", "source": "ntsearch"}

    called.clear()
    node = object()
    assert await search_track("晴天", "netease", node=node) == []
    assert called == {"query": "晴天", "source": "ntsearch", "node": node}

    called.clear()
    link = "https://open.spotify.com/track/abc"
    assert await search_track(link, "youtube") == []
    assert called == {"query": link, "source": None}

async def test_a_silent_link_keeps_its_title(monkeypatch):
    """Apple writes the title in the path. Spotify's page does, once Lavalink has not."""
    assert await heard_title("https://music.apple.com/us/album/animals/1?i=2") == "animals"

    import aiohttp

    class Page:
        status = 200
        requested = []

        def __init__(self):
            self.requested = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def json(self, content_type=None):
            return {"title": "One Wish", "author_name": "Ray J"}

    class Session:
        def __init__(self, page):
            self.page = page

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def get(self, url, params=None):
            self.page.requested.append((url, dict(params or {})))
            return self.page

    page = Page()
    monkeypatch.setattr(aiohttp, "ClientSession", lambda *args, **kwargs: Session(page))
    assert await heard_title("https://open.spotify.com/track/abc") == "One Wish Ray J"
    assert page.requested == [(
        "https://open.spotify.com/oembed",
        {"url": "https://open.spotify.com/track/abc", "format": "json"},
    )]
