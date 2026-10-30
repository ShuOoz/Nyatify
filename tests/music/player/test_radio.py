"""Last.fm names a similar song, and the key box turns radio on. No Discord login and no network."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import discord

from music.playback import _stored, now_playing, queue_tracks, stop_track
from music.player.card import MusicPlayer
from music.player.radio import radio_seed, read_similar, similar_songs

from tests.music.support import _named, _playing, _press


async def test_radio_names_a_similar_song(monkeypatch):
    """The seed keeps a real artist. A bad answer, or no key, queues nothing."""
    assert radio_seed("It's Always You", "Chet Baker") == ("Chet Baker", "It's Always You")
    assert radio_seed("It's Always You", "Chet Baker - Topic") == ("Chet Baker", "It's Always You")
    assert radio_seed(
        "Chet Baker - It's Always You (Official Audio)",
        "ChetBakerVEVO",
    ) == ("Chet Baker", "It's Always You")
    assert radio_seed("It's Always You (Live)", "Chet Baker") == ("Chet Baker", "It's Always You (Live)")
    assert radio_seed("It's Always You", "") is None
    assert radio_seed("Air on the G String", "Air") == ("Air", "Air on the G String")
    assert radio_seed("Wait", "AI") == ("AI", "Wait")
    assert radio_seed("Chet Baker - It's Always You", "Chet Baker") == ("Chet Baker", "It's Always You")

    payload = {
        "similartracks": {
            "track": [
                {"name": "Peace Piece", "artist": {"name": "Bill Evans"}},
                {"name": "", "artist": {"name": "Nobody"}},
            ]
        }
    }
    assert read_similar(payload) == [("Bill Evans", "Peace Piece")]
    assert read_similar({"similartracks": {"track": {"name": "So What", "artist": {"name": "Miles Davis"}}}}) == [
        ("Miles Davis", "So What"),
    ]
    assert read_similar({"error": 6, "message": "missing"}) == []
    assert read_similar([]) == []

    monkeypatch.setattr("music.player.radio.settings.lastfm_api_key", None)

    async def unused(_url: str) -> str:
        raise AssertionError("no key")

    assert await similar_songs("Chet Baker", "It's Always You", getter=unused) == []

    monkeypatch.setattr("music.player.radio.settings.lastfm_api_key", "fm-key")
    seen: list[str] = []

    async def getter(url: str) -> str:
        seen.append(url)
        return json.dumps({
            "similartracks": {"track": [{"name": "Peace Piece", "artist": {"name": "Bill Evans"}}]}
        })

    assert await similar_songs("Chet Baker", "It's Always You", getter=getter) == [
        ("Bill Evans", "Peace Piece"),
    ]
    assert "method=track.getsimilar" in seen[0]
    assert "artist=Chet+Baker" in seen[0]
    assert "autocorrect=1" in seen[0]
    assert "api_key=fm-key" in seen[0]

    async def broken(_url: str) -> str:
        raise TimeoutError("slow")

    assert await similar_songs("Chet Baker", "It's Always You", getter=broken) == []

    async def error(_url: str) -> str:
        return json.dumps({"error": 10, "message": "Invalid API key"})

    assert await similar_songs("Chet Baker", "It's Always You", getter=error) == []

async def test_radio_without_a_key_opens_the_key_box(monkeypatch):
    guild, player, _track = await _playing()
    try:
        from config import settings

        view = MusicPlayer(guild)
        outsider = MagicMock()
        outsider.send = AsyncMock()
        outsider.user.voice = None
        outsider.response.send_modal = AsyncMock()
        await view.radio.callback(outsider)
        outsider.response.send_modal.assert_not_awaited()
        assert "Join the voice channel first" in outsider.send.await_args.args[0]

        press = _press(player)
        press.response.send_modal = AsyncMock()
        await view.radio.callback(press)
        modal = press.response.send_modal.await_args.args[0]
        assert modal.title == "Last.fm"

        empty = _press(player)
        empty.send = AsyncMock()
        modal.key.component._value = "short"
        await modal.on_submit(empty)
        assert "Paste a Last.fm API key" in empty.send.await_args.args[0]
        assert settings.lastfm_api_key is None

        written = {}
        monkeypatch.setattr(
            "music.player.radio.upsert_config_values",
            lambda _path, values: written.update(values),
        )
        monkeypatch.setattr("music.player.radio.similar_songs", AsyncMock(return_value=[]))
        monkeypatch.setattr("music.source.search_track", AsyncMock(return_value=[]))
        member = MagicMock()
        member.id = 18
        member.voice.channel = player.channel
        assert await queue_tracks(member, guild, [_named("After")]) == "Added After."
        submit = _press(player)
        modal.key.component._value = "abc123abc123abc1"
        await modal.on_submit(submit)
        assert written == {"LASTFM_API_KEY": "abc123abc123abc1"}
        assert settings.lastfm_api_key == "abc123abc123abc1"
        assert _stored(guild).radio is True
        assert view.radio.style == discord.ButtonStyle.primary
        assert "On Radio" not in view.meta.content
        assert now_playing(guild).title == "I Ain't Worried"
        assert view.meta.content == "-# On Queue\n-# Up next: A similar song"
        assert view.shuffle.disabled is True
        assert view.repeat.disabled is False
        assert [item.track.title for item in _stored(guild).later] == ["After"]

        await view.repeat.callback(submit)
        assert _stored(guild).repeat is True
        assert _stored(guild).radio is True
        assert view.repeat.style == discord.ButtonStyle.primary
        assert view.meta.content == "-# On Repeat\n-# Up next: After"

        await view.shuffle.callback(submit)
        assert _stored(guild).shuffle is False

        again = _press(player)
        again.response.send_modal = AsyncMock()
        await view.radio.callback(again)
        again.response.send_modal.assert_not_awaited()
        assert _stored(guild).radio is False
        assert _stored(guild).repeat is True
        assert view.radio.style == discord.ButtonStyle.secondary
        assert view.shuffle.disabled is False
        assert view.meta.content == "-# On Repeat\n-# Up next: After"
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)
