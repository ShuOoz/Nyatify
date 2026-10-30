"""Similar songs from Last.fm, and the radio button that asks for the key."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from urllib.parse import urlencode

import discord

from announce import notice_text, respond
from config import CONFIG_PATH, settings, upsert_config_values

log = logging.getLogger(__name__)

_ROOT = "https://ws.audioscrobbler.com/2.0/"
_AGENT = "Nyatify"
_LIMIT = 5
# Official-upload tags. A live or remaster mark stays, because that is the song.
_TAG = re.compile(
    r"\s*[(\[][^)\]]*\b(?:official|audio|video|lyric|lyrics|visualizer|hd|hq|4k|mv)\b[^)\]]*[)\]]",
    re.IGNORECASE,
)
_TOPIC = re.compile(r"\s*-\s*topic\s*$", re.IGNORECASE)

Getter = Callable[[str], Awaitable[str]]


def saved_key() -> str:
    """The Last.fm key already loaded. Empty when radio still needs one."""
    return (getattr(settings, "lastfm_api_key", None) or "").strip()


def store_key(key: str) -> None:
    """Keep the key for this process and write it for the next start."""
    settings.lastfm_api_key = key
    upsert_config_values(CONFIG_PATH, {"LASTFM_API_KEY": key})


def _words_in(name: str, text: str) -> bool:
    """True when every word of `name` is a whole-word run inside `text`."""
    words = name.casefold().split()
    hay = text.casefold().split()
    size = len(words)
    if not size or size > len(hay):
        return False
    return any(hay[index:index + size] == words for index in range(len(hay) - size + 1))


def radio_seed(title: str, author: str) -> tuple[str, str] | None:
    """Artist and title Last.fm can match. None when the artist is missing."""
    title = _clean(title)
    author = _clean(_TOPIC.sub("", author))
    if not title:
        return None
    split = _split_title(title)
    # "Artist - Song" already names the artist. A channel, or that artist copied
    # into the author field, should use the split. The artist can still be a word
    # in the song: Air is the artist of Air on the G String, and AI of Wait.
    if split is not None and (not author or _channel(author) or _words_in(author, title)):
        return split
    if author and author.casefold() != title.casefold():
        return author, title
    return split


def read_similar(payload: object) -> list[tuple[str, str]]:
    """Artist and title pairs. An error body is an empty list."""
    if not isinstance(payload, dict) or "error" in payload:
        return []
    block = payload.get("similartracks")
    tracks = block.get("track") if isinstance(block, dict) else None
    if isinstance(tracks, dict):
        tracks = [tracks]
    if not isinstance(tracks, list):
        return []
    found: list[tuple[str, str]] = []
    for row in tracks:
        if not isinstance(row, dict):
            continue
        name = _words(row.get("name"))
        artist = row.get("artist")
        artist_name = _words(artist.get("name")) if isinstance(artist, dict) else ""
        if name and artist_name:
            found.append((artist_name, name))
    return found


async def similar_songs(artist: str, title: str, *, getter: Getter | None = None) -> list[tuple[str, str]]:
    """Up to five similar tracks. No key, a miss, or a failed call is an empty list."""
    key = saved_key()
    artist, title = _words(artist), _words(title)
    if not key or not artist or not title:
        return []
    query = urlencode({
        "method": "track.getsimilar",
        "artist": artist,
        "track": title,
        "api_key": key,
        "format": "json",
        "autocorrect": "1",
        "limit": str(_LIMIT),
    })
    try:
        body = await (getter or _get)(f"{_ROOT}?{query}")
        payload = json.loads(body)
    except Exception:
        log.warning("Last.fm similar tracks failed", exc_info=True)
        return []
    return read_similar(payload)


def _split_title(title: str) -> tuple[str, str] | None:
    if " - " not in title:
        return None
    artist, name = title.split(" - ", 1)
    artist, name = artist.strip(), _clean(name)
    if artist and name:
        return artist, name
    return None


def _channel(author: str) -> bool:
    folded = author.casefold()
    return folded.endswith("vevo") or "official" in folded


def _clean(text: str) -> str:
    return _words(_TAG.sub("", text))


def _words(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())


async def _get(url: str) -> str:
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=8)
    headers = {"User-Agent": _AGENT, "Accept": "application/json"}
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers=headers) as response:
            if response.status >= 400:
                return ""
            return await response.text()


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
        if not self._inside(interaction):
            await respond(interaction, notice_text("Join the voice channel first."), ephemeral=True)
            return
        if not saved_key():
            await interaction.response.send_modal(_Lastfm(self._apply_key))
            return
        await self._turn_on(interaction)

    async def _apply_key(self, interaction: discord.Interaction) -> None:
        await self._turn_on(interaction)

    async def _turn_on(self, interaction: discord.Interaction) -> None:
        # Playback imports this module while it is still starting.
        from music.playback import toggle_radio

        await self._act(interaction, lambda: toggle_radio(self.guild, interaction.user), "radio")
