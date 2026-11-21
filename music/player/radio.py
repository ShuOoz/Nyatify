"""Similar songs from Last.fm, and the radio button that asks for the key."""

from __future__ import annotations

import json
import logging
import random
import re
from collections.abc import Awaitable, Callable
from urllib.parse import urlencode

import discord

from announce import notice_text, respond
from config import CONFIG_PATH, settings, upsert_config_values

log = logging.getLogger(__name__)

_TOPIC = re.compile(r"\s*-\s*topic\s*$", re.IGNORECASE)
# A curly apostrophe is still an apostrophe. Dropping it lets Don't match Don’t.
_APOSTROPHE = str.maketrans({"'": "", "\u2019": "", "\u2018": "", "`": "", "\u00b4": ""})
_DASH = str.maketrans({"-": " ", "\u2010": " ", "\u2011": " ", "\u2013": " ", "\u2014": " "})
# Official-upload tags. A live or remaster mark stays, because that is the song.
_TAG = re.compile(
    r"\s*[(\[][^)\]]*\b(?:official|audio|video|lyric|lyrics|visualizer|hd|hq|4k|mv)\b[^)\]]*[)\]]",
    re.IGNORECASE,
)
_ROOT = "https://ws.audioscrobbler.com/2.0/"
_AGENT = "Nyatify"
# A handful is the same songs every time. Thirty is a station to draw from.
_LIMIT = 30
_SIMILAR_TRIES = 3
# Other songs and a nearby artist are a step, not the next station.
_FALLBACK = 8

Getter = Callable[[str], Awaitable[str]]

def _words(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())

def _word_list(text: str) -> list[str]:
    """Words for a match. Apostrophes drop, and & is the word and."""
    folded = text.casefold().translate(_APOSTROPHE).replace("&", " and ").translate(_DASH)
    words: list[str] = []
    for raw in folded.split():
        word = "".join(char for char in raw if char.isalnum())
        if word:
            words.append(word)
    return words

def _words_in(name: str, text: str) -> bool:
    """True when every word of `name` is a whole-word run inside `text`."""
    words = _word_list(name)
    hay = _word_list(text)
    size = len(words)
    if not size or size > len(hay):
        return False
    return any(hay[index:index + size] == words for index in range(len(hay) - size + 1))

def _same_name(left: str, right: str) -> bool:
    words = _word_list(left)
    return bool(words) and words == _word_list(right)

def bare_author(author: str) -> str:
    """The name before a YouTube Topic suffix."""
    return _TOPIC.sub("", author).strip()

def channel_author(author: str) -> bool:
    """A channel credit, not the artist. VEVO, an official upload, or a Topic page."""
    folded = author.casefold()
    if folded.endswith("vevo") or "official" in folded:
        return True
    bare = bare_author(author)
    return bool(bare) and bare.casefold() != folded

def same_song(title: str, artist: str, name: str, who: str, *, channel: bool = False) -> bool:
    """True when this result is the song being looked up.

    A channel upload can carry the title without the artist as its own word.
    A site switch still requires the artist.
    """
    if not (_same_name(name, title) or _words_in(title, name)):
        return False
    if not artist:
        return True
    if _same_name(who, artist) or _words_in(artist, who) or _words_in(artist, name):
        return True
    return channel and (not who or channel_author(who))

def same_title(wanted: str, found: str) -> bool:
    """The result title is this song. Air is a whole word, so it stays off Airplane."""
    found = _words(found)
    return _same_name(found, wanted) or _words_in(wanted, found)

def weighted_order(weights: list[float], rolls: list[float]) -> list[int]:
    """Indexes, a larger weight tending to come first.

    `rolls` are draws in (0, 1], one per weight, so a test can force the order.
    A non-positive weight still has a small chance.
    """
    keyed: list[tuple[float, int]] = []
    for index, weight in enumerate(weights):
        roll = rolls[index] if index < len(rolls) else 1.0
        if isinstance(roll, bool) or not isinstance(roll, (int, float)):
            roll = 1.0
        roll = min(max(float(roll), 1e-12), 1.0)
        safe = weight if isinstance(weight, (int, float)) and not isinstance(weight, bool) and weight > 0 else 0.05
        keyed.append((roll ** (1.0 / safe), index))
    keyed.sort(key=lambda item: item[0], reverse=True)
    return [index for _score, index in keyed]

def saved_key() -> str:
    """The Last.fm key already loaded. Empty when radio still needs one."""
    return (getattr(settings, "lastfm_api_key", None) or "").strip()

def store_key(key: str) -> None:
    """Keep the key for this process and write it for the next start."""
    settings.lastfm_api_key = key
    upsert_config_values(CONFIG_PATH, {"LASTFM_API_KEY": key})

def read_similar(payload: object) -> list[tuple[str, str]]:
    """Artist and title pairs. An error body is an empty list."""
    return _track_rows(payload, "similartracks")

def read_top(payload: object) -> list[tuple[str, str]]:
    """Artist and title pairs from an artist's known songs."""
    return _track_rows(payload, "toptracks")

def read_artists(payload: object) -> list[str]:
    """Artist names. An error body is an empty list."""
    names: list[str] = []
    for row in _rows(payload, "similarartists", "artist"):
        name = _words(row.get("name"))
        if name:
            names.append(name)
    return names

def _scored(payload: object, key: str) -> list[tuple[str, str, float]]:
    return _track_rows(payload, key, score=True)

def _track_rows(payload: object, key: str, *, score: bool = False):
    """Artist and title from each track row. `score` keeps Last.fm's match."""
    found = []
    for row in _rows(payload, key, "track"):
        name = _words(row.get("name"))
        artist = row.get("artist")
        artist_name = _words(artist.get("name")) if isinstance(artist, dict) else ""
        if not name or not artist_name:
            continue
        if score:
            found.append((artist_name, name, _match(row.get("match"))))
        else:
            found.append((artist_name, name))
    return found

def _rows(payload: object, key: str, item: str) -> list[dict]:
    if not isinstance(payload, dict) or "error" in payload:
        return []
    block = payload.get(key)
    rows = block.get(item) if isinstance(block, dict) else None
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]

def _match(value: object) -> float:
    if isinstance(value, bool):
        return 0.05
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.05
    if number <= 0:
        return 0.05
    return number

async def _load(method: str, fields: dict[str, str], getter: Getter | None) -> dict:
    key = saved_key()
    if not key or any(not value for value in fields.values()):
        return {}
    query = urlencode({
        "method": method,
        **fields,
        "api_key": key,
        "format": "json",
        "autocorrect": "0",
        "limit": str(_LIMIT),
    })
    try:
        body = await (getter or _get)(f"{_ROOT}?{query}")
        payload = json.loads(body)
    except Exception:
        log.warning("Last.fm %s failed", method, exc_info=True)
        return {}
    return payload if isinstance(payload, dict) else {}

async def _get(url: str) -> str:
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=8)
    headers = {"User-Agent": _AGENT, "Accept": "application/json"}
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers=headers) as response:
            if response.status >= 400:
                return ""
            return await response.text()

def radio_seed(title: str, author: str) -> tuple[str, str] | None:
    """Artist and title Last.fm can match. None when the artist is missing."""
    title = _clean(title)
    author = _clean(bare_author(author))
    if not title:
        return None
    split = _split_title(title)
    # "Artist - Song" already names the artist. A channel, or that artist copied
    # into the author field, should use the split. The artist can still be a word
    # in the song: Air is the artist of Air on the G String, and AI of Wait.
    if split is not None and (not author or channel_author(author) or _words_in(author, title)):
        return split
    if author and author.casefold() != title.casefold():
        return author, title
    return split

def _split_title(title: str) -> tuple[str, str] | None:
    if " - " not in title:
        return None
    artist, name = title.split(" - ", 1)
    artist, name = artist.strip(), _clean(name)
    if artist and name:
        return artist, name
    return None

def _clean(text: str) -> str:
    return _words(_TAG.sub("", text))

async def load_similar(
    artist: str, title: str, *, getter: Getter | None = None,
) -> list[tuple[str, str, float]]:
    """Similar tracks with Last.fm's match, closest first. A miss is an empty list."""
    artist, title = _words(artist), _words(title)
    return _scored(await _load("track.getsimilar", {"artist": artist, "track": title}, getter), "similartracks")

async def similar_songs(artist: str, title: str, *, getter: Getter | None = None) -> list[tuple[str, str]]:
    """Similar tracks to draw from. No key, a miss, or a failed call is an empty list."""
    return [(artist_name, name) for artist_name, name, _weight in await load_similar(artist, title, getter=getter)]

async def top_tracks(artist: str, *, getter: Getter | None = None) -> list[tuple[str, str]]:
    """This artist's known songs. No key, a miss, or a failed call is an empty list."""
    return read_top(await _load("artist.gettoptracks", {"artist": _words(artist)}, getter))

async def similar_artists(artist: str, *, getter: Getter | None = None) -> list[str]:
    """Artists listeners also play. No key, a miss, or a failed call is an empty list."""
    return read_artists(await _load("artist.getsimilar", {"artist": _words(artist)}, getter))

def station_heard(songs) -> set[tuple[str, str]]:
    """Artist and title pairs already in this station, so the next draw can skip them."""
    from music.playback.remember import _text

    found: set[tuple[str, str]] = set()
    for song in songs:
        track = getattr(song, "track", None)
        pair = radio_seed(
            _text(getattr(track, "title", None)),
            _text(getattr(track, "author", None)),
        )
        if pair is not None:
            found.add((pair[0].casefold(), pair[1].casefold()))
    return found

def _heard_query(title: str, author: str) -> str:
    """Title, plus the artist when that name is not already a whole word in it.

    "Air" is not inside "Airplane". A title that already names the artist
    searches as that title alone.
    """
    if not author or _words_in(author, title):
        return title
    return f"{title} {author}"

def _seed_of(song) -> tuple[str, str] | None:
    from music.playback.remember import _text

    title = _text(getattr(song.track, "title", None), "Unknown")
    author = _text(getattr(song.track, "author", None))
    return radio_seed(title, author)

async def choose_next(song, *, heard: set[tuple[str, str]] | None = None) -> tuple[str, str] | None:
    """The next name for this song. The card can show it before it is searched."""
    pair = _seed_of(song)
    if pair is None:
        return None
    async for choices in _batches(*pair, heard or set()):
        if choices:
            return choices[0]
    return None

async def another_track(
    song,
    *,
    heard: set[tuple[str, str]] | None = None,
    preferred: tuple[str, str] | None = None,
) -> object | None:
    """The next song from the one playing.

    Similar songs are drawn at random, and a closer match comes up more often.
    When none of them can be played, a few of this artist's other songs are
    tried, then a nearby artist. The song after that is drawn again. A song
    already heard stays out. `preferred` is tried first: it is the song a back
    press just left.
    """
    skip = set(heard or ())
    if preferred is not None:
        artist, name = preferred
        key = (artist.casefold(), name.casefold())
        if key not in skip:
            track = await _search_named(song, name, artist)
            if track is not None:
                return track
            skip.add(key)
    pair = _seed_of(song)
    if pair is None:
        return None
    first = True
    async for choices in _batches(*pair, skip):
        # The similar list is the station. Later lists are only a step.
        track = await _take(song, choices if first else choices[:_FALLBACK])
        first = False
        if track is not None:
            return track
    return None

async def _batches(artist: str, name: str, skip: set[tuple[str, str]]):
    """Similar songs, then this artist's other songs, then a nearby artist.

    A step is built only when the caller asks for the next one.
    """
    yield await _similar_names(artist, name, skip)
    others = _keep(await top_tracks(artist), artist, name, skip)
    random.shuffle(others)
    yield others
    nearby = await _neighbor(artist, name, skip)
    random.shuffle(nearby)
    yield nearby

async def _similar_names(artist: str, name: str, skip: set[tuple[str, str]]) -> list[tuple[str, str]]:
    scored = []
    for row in await load_similar(artist, name):
        weight = row[2] if len(row) >= 3 else 1.0
        scored.append((row[0], row[1], _match(weight)))
    scored = _keep(scored, artist, name, skip)
    order = weighted_order(
        [weight for _artist, _title, weight in scored],
        [random.random() for _row in scored],
    )
    return [(scored[index][0], scored[index][1]) for index in order]

def _keep(rows, artist: str, name: str, skip: set[tuple[str, str]] | None = None):
    """Drop this song and anything the station already heard."""
    here = (artist.casefold(), name.casefold())
    passed = skip or set()
    kept = []
    for row in rows:
        key = (row[0].casefold(), row[1].casefold())
        if key != here and key not in passed:
            kept.append(row)
    return kept

async def _neighbor(artist: str, name: str, skip: set[tuple[str, str]]) -> list[tuple[str, str]]:
    """Songs by a nearby artist. This artist's own list has already run out."""
    names = [item for item in await similar_artists(artist) if item.casefold() != artist.casefold()]
    random.shuffle(names)
    for other in names[:_SIMILAR_TRIES]:
        found = _keep(await top_tracks(other), artist, name, skip)
        if found:
            random.shuffle(found)
            return found
    return []

async def _take(song, choices: list[tuple[str, str]]):
    """The first of these songs that plays."""
    for other_artist, other_name in choices:
        track = await _search_named(song, other_name, other_artist)
        if track is not None:
            return track
    return None

def radio_platform(platform: str) -> str:
    """The site this name is searched on. NetEase is not on Lavalink, so the name goes to Apple Music."""
    from music import source

    chosen = source.normalize_platform(platform)
    if chosen == "netease":
        return source.NAME_SEARCH_PLATFORM
    return chosen

def _places(platform: str) -> list[str]:
    """This song's site, then YouTube when that site has no recording."""
    first = radio_platform(platform)
    if first == "youtube":
        return [first]
    return [first, "youtube"]

def _mark(track, platform: str):
    try:
        setattr(track, "_found_on", platform)
    except Exception:
        return track
    return track

def found_platform(track, fallback: str) -> str:
    """The site the recording was found on. The song that was playing is the fallback."""
    marked = getattr(track, "_found_on", None)
    if isinstance(marked, str) and marked:
        return marked
    return radio_platform(fallback)

async def _search_named(song, title: str, artist: str):
    import wavelink

    from music import source
    from music.lavalink import JAVA_NODE, _named_node
    from music.playback.remember import _each

    # Radio stays on Lavalink. The free node in the pool is NodeLink once a
    # song is playing, and that node cannot search this name.
    node = _named_node(JAVA_NODE)
    extra = {} if node is None else {"node": node}
    query = f"{title} {artist}"
    for platform in _places(song.platform):
        try:
            found = await source.search_track(query, platform, **extra)
        except wavelink.WavelinkException:
            log.exception("Similar song search failed")
            continue
        tracks = _each(found)
        if platform == "soundcloud":
            tracks = source.drop_previews(tracks)
        chosen = _chosen(tracks, title, artist, song.track)
        if chosen is not None:
            return _mark(chosen, platform)
    return None

def _chosen(tracks, title: str, _artist: str, current) -> object | None:
    """The first result whose title is this song.

    The search already included the artist, so the site ranks Knx. against
    Knxwledge. A different title stays out, and so does the recording already playing.
    """
    from music.playback.remember import _same_track

    return next(
        (
            track for track in tracks
            if not _same_track(track, current) and same_title(title, getattr(track, "title", None))
        ),
        None,
    )

def _lastfm_key(text: str) -> str | None:
    cleaned = "".join(text.split())
    if len(cleaned) < 16 or len(cleaned) > 64 or not cleaned.isalnum():
        return None
    return cleaned

class _Lastfm(discord.ui.Modal):
    """The key radio needs. Saving it turns radio on."""

    key = discord.ui.Label(
        text="API key",
        description="From last.fm/api/account/create",
        component=discord.ui.TextInput(placeholder="Last.fm API key", max_length=64),
    )

    def __init__(self, apply: Callable[[discord.Interaction], Awaitable[None]]) -> None:
        super().__init__(title="Last.fm")
        self._apply = apply

    async def on_submit(self, interaction: discord.Interaction) -> None:
        key = _lastfm_key(str(self.key.component))
        if key is None:
            await respond(interaction, notice_text("Paste a Last.fm API key."), ephemeral=True)
            return
        try:
            store_key(key)
        except Exception:
            log.exception("could not save the Last.fm key")
            await respond(interaction, notice_text("Could not save the Last.fm key."), ephemeral=True)
            return
        await self._apply(interaction)

class _Radio:
    """The radio button. With no key, it asks for one first."""

    async def on_radio(self, interaction: discord.Interaction) -> None:
        """Turn radio on. With no key, the button asks for one first."""
        if not self._can_edit(interaction):
            await respond(interaction, notice_text("Join the voice channel first."), ephemeral=True)
            return
        if not saved_key():
            await interaction.response.send_modal(_Lastfm(self._turn_on))
            return
        await self._turn_on(interaction)

    async def _turn_on(self, interaction: discord.Interaction) -> None:
        # Playback imports this module while it is still starting.
        from music.playback import toggle_radio

        await self._act(interaction, lambda: toggle_radio(self.guild, interaction.user), "radio", room=False)
