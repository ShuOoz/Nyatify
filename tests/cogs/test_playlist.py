"""Saved playlists and the /music playlist card. No Discord login and no Lavalink node."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord

from cogs.osa_musify import OsaMusify
from guild import GuildConfig, guild_config
from music.library import (
    PLAYLISTS,
    SavedList,
    SavedSong,
    find_playlist,
    list_playlists,
    page_bounds,
    playlist_name,
    save_song,
)
from music.commands.playlist import LiveLine, SaveSong as SaveSongModal, SavedPlaylist, line_text, saved_text
from music.commands.player import MusicPlayer
from music.lavalink import Found, Opened, open_recordings
from music.playback import (
    _stored,
    drop_queued,
    load_line,
    play_now,
    queue_tracks,
    queued,
    stop_track,
)
from tests.cogs.test_music import _Player, _ask, _named


def test_a_saved_song_keeps_its_link(tmp_path):
    assert playlist_name("  evening   jazz ") == "evening jazz"
    assert playlist_name("   ") is None
    assert playlist_name("e" * 100) == "e" * 80
    assert page_bounds(0, 3) == (0, 0, 0)
    assert page_bounds(12, 1) == (10, 12, 1)
    assert page_bounds(12, 9) == (10, 12, 1)

    store = GuildConfig(tmp_path / "guild.json")
    song = SavedSong("One Wish", "YouTube", "https://example.com/one")
    assert save_song(store, 7, "   ", song, 4) == "Name the playlist."
    assert save_song(store, 7, "Evening", SavedSong("Nope", "youtube", "notaurl"), 4) == (
        "That song has no link to save."
    )
    created = save_song(store, 7, "Evening", song, 4)
    assert isinstance(created, SavedList)
    again = save_song(
        store, 7, "  evening ", SavedSong("Two", "spotify", "https://example.com/two"), 9,
    )
    assert isinstance(again, SavedList)
    assert again.name == "Evening"
    assert again.saver_id == 4
    assert [(item.title, item.platform) for item in again.songs] == [
        ("One Wish", "youtube"),
        ("Two", "spotify"),
    ]
    reloaded = GuildConfig(store.path)
    assert find_playlist(reloaded, 7, "EVENING").songs == again.songs

    store.set_guild_list(8, PLAYLISTS, [
        "nope",
        {"name": " ", "songs": []},
        {
            "name": "Night",
            "saver_id": True,
            "songs": [
                {"title": "Drop", "platform": "youtube", "url": "ftp://x"},
                {"title": "  One Wish  ", "platform": "Spotify", "url": "https://example.com/a"},
            ],
        },
    ])
    found = list_playlists(store, 8)
    assert len(found) == 1
    assert found[0].name == "Night"
    assert found[0].saver_id is None
    assert found[0].songs == (SavedSong("One Wish", "spotify", "https://example.com/a"),)


def test_line_pages_name_the_current_song():
    titles = [f"Song {index}" for index in range(1, 13)]
    text = line_text(titles, {0}, 0)
    assert text.startswith("**Line**\n1. **Song 1**\n2. Song 2\n")
    assert "10. Song 10" in text
    assert "11. Song 11" not in text
    assert text.endswith("Page 1 of 2")
    assert "11. Song 11" in line_text(titles, set(), 1)
    assert line_text([], set(), 0) == "**Line**\nNothing is left in the line."
    saved = SavedList("Evening", 17, (SavedSong("One", "youtube", "https://example.com/a"),))
    assert saved_text(saved, 0) == "**Evening**\n<@17>\n1. One"
    empty = SavedList("Evening", None, ())
    assert saved_text(empty, 0) == "**Evening**\nEvening has no songs."


async def _line_up(guild_id: int, titles: list[str]):
    guild = MagicMock()
    guild.id = guild_id
    guild.voice_client = None
    await stop_track(guild)
    channel = MagicMock()
    channel.id = guild_id + 1
    player = _Player(channel)
    channel.connect = AsyncMock(return_value=player)

    async def play(track):
        player._track = track

    player.play.side_effect = play
    member = MagicMock()
    member.id = guild_id + 2
    member.voice.channel = channel
    tracks = []
    for title in titles:
        track = _named(title)
        track.uri = f"https://example.com/{title.replace(' ', '-').lower()}"
        tracks.append(track)
    note = await queue_tracks(member, guild, tracks)
    guild.voice_client = player
    return guild, member, player, tracks, note


async def test_the_live_line_can_drop_a_song_or_start_another():
    guild, member, player, tracks, note = await _line_up(9100, ["Alpha", "Beta"])
    try:
        assert note == "Playing Alpha."
        rows = queued(guild)
        assert [(row.title, row.playing, row.url) for row in rows] == [
            ("Alpha", True, "https://example.com/alpha"),
            ("Beta", False, "https://example.com/beta"),
        ]
        assert await drop_queued(guild, 1) is None
        assert [row.title for row in queued(guild)] == ["Alpha"]
        assert await drop_queued(guild, 0) is None
        assert queued(guild) == []
        player.disconnect.assert_not_awaited()
        await stop_track(guild)

        guild, member, player, tracks, _note = await _line_up(9101, ["Alpha", "Beta"])
        charlie, delta = _named("Charlie"), _named("Delta")
        assert await load_line(member, guild, [(charlie, "spotify"), (delta, "youtube")]) == (
            "Playing Charlie."
        )
        assert [row.title for row in queued(guild)] == ["Charlie", "Delta"]
        assert queued(guild)[1].platform == "youtube"
        assert [song.track.title for song in _stored(guild).earlier] == ["Alpha"]

        echo = _named("Echo")
        assert await play_now(member, guild, echo, "soundcloud") == "Playing Echo."
        assert [row.title for row in queued(guild)] == ["Echo", "Delta"]
        assert [song.track.title for song in _stored(guild).earlier] == ["Alpha", "Charlie"]

        outsider = MagicMock()
        outsider.voice.channel = MagicMock()
        outsider.voice.channel.id = 1
        played = player.play.await_count
        assert await play_now(outsider, guild, _named("Nope"), "youtube") == (
            "Join the voice channel first."
        )
        assert player.play.await_count == played
        assert [row.title for row in queued(guild)] == ["Echo", "Delta"]
    finally:
        await stop_track(guild)


async def test_a_saved_link_opens_one_recording(monkeypatch):
    calls = []

    async def search(guild, song, platform="youtube", mode=None):
        calls.append((song, platform, mode))
        if "missing" in song:
            return "Nothing on YouTube matched that."
        return Found(song, platform, [_named("Kept")], True)

    monkeypatch.setattr("music.lavalink.find_songs", search)
    guild = MagicMock()
    guild.id = 44
    songs = [
        SavedSong("A", "spotify", "https://open.spotify.com/a"),
        SavedSong("B", "youtube", "https://missing.example/b"),
        SavedSong("C", "apple", "https://music.apple.com/c"),
    ]
    opened = await open_recordings(guild, songs)
    assert isinstance(opened, Opened)
    assert [(track.title, platform) for track, platform in opened.tracks] == [
        ("Kept", "spotify"),
        ("Kept", "apple"),
    ]
    assert opened.skipped == ["B"]
    assert calls == [
        ("https://open.spotify.com/a", "spotify", "track"),
        ("https://missing.example/b", "youtube", "track"),
        ("https://music.apple.com/c", "apple", "track"),
    ]
    assert await open_recordings(guild, [SavedSong("A", "youtube", "not-a-link")]) == (
        "That song has no link to play."
    )
    search_fail = AsyncMock(return_value="Nothing on YouTube matched that.")
    monkeypatch.setattr("music.lavalink.find_songs", search_fail)
    assert await open_recordings(guild, songs) == "Nothing on YouTube matched that."


def _press(user=None):
    interaction = _ask()
    interaction.response.edit_message = AsyncMock()
    interaction.response.send_modal = AsyncMock()
    if user is not None:
        interaction.user = user
    return interaction


async def test_the_live_line_pages_removes_and_saves(tmp_path):
    titles = [f"Song {index}" for index in range(1, 13)]
    guild, member, _player, _tracks, note = await _line_up(9200, titles)
    store = GuildConfig(tmp_path / "guild.json")
    try:
        assert note == "Playing Song 1."
        view = LiveLine(guild, store)
        view.to_components()
        assert view.body.content.startswith("**Line**\n1. **Song 1**\n")
        assert "Page 1 of 2" in view.body.content
        assert [option.value for option in view.remove.options] == [str(index) for index in range(10)]

        press = _press(member)
        await view.on_forward(press)
        assert "11. Song 11" in view.body.content
        assert "12. Song 12" in view.body.content
        assert [option.value for option in view.remove.options] == ["10", "11"]

        press = _press()
        view.remove._values = ["11"]
        await view.on_remove(press)
        assert "Join the voice channel first" in press.send.await_args.args[0]
        assert any(row.title == "Song 12" for row in queued(guild))

        press = _press(member)
        view.remove._values = ["11"]
        await view.on_remove(press)
        assert "Song 12" not in view.body.content
        assert [row.title for row in queued(guild)][-1] == "Song 11"

        view.save._values = ["0"]
        first = queued(guild)[0]
        first_track = _stored(guild).current.track
        first_track.uri = None
        view.paint()
        press = _press(member)
        await view.on_save(press)
        assert "That song has no link to save" in press.send.await_args.args[0]
        press.response.send_modal.assert_not_awaited()

        first_track.uri = "https://example.com/song-1"
        view.paint()
        press = _press(member)
        view.save._values = ["0"]
        await view.on_save(press)
        modal = press.response.send_modal.await_args.args[0]
        assert isinstance(modal, SaveSongModal)
        modal.playlist.component._value = "Evening"
        submit = _press()
        await modal.on_submit(submit)
        assert "Saved Song 1 to Evening" in submit.send.await_args.args[0]
        saved = find_playlist(store, guild.id, "evening")
        assert saved is not None
        assert saved.name == "Evening"
        assert saved.saver_id == member.id
        assert saved.songs[0].url == "https://example.com/song-1"

        modal.playlist.component._value = "   "
        blank = _press()
        await modal.on_submit(blank)
        assert "Name the playlist" in blank.send.await_args.args[0]
    finally:
        await stop_track(guild)


async def test_a_saved_list_plays_in_order_or_one_song(monkeypatch):
    saved = SavedList(
        "Evening",
        17,
        (
            SavedSong("One", "youtube", "https://example.com/one"),
            SavedSong("Two", "spotify", "https://example.com/two"),
        ),
    )
    started = AsyncMock()

    async def opened(_interaction):
        assert view.is_finished()

    started.side_effect = opened
    loaded = []

    async def open_all(_guild, songs):
        return Opened([(_named(song.title), song.platform) for song in songs], [])

    async def start(_member, _guild, tracks):
        loaded.append([(track.title, platform) for track, platform in tracks])
        return f"Playing {tracks[0][0].title}."

    monkeypatch.setattr("music.commands.playlist.open_recordings", open_all)
    monkeypatch.setattr("music.commands.playlist.load_line", start)
    view = SavedPlaylist(saved, started)
    view.to_components()
    assert view.body.content == "**Evening**\n<@17>\n1. One\n2. Two"
    labels = [item.label for item in view.walk_children() if isinstance(item, discord.ui.Button)]
    assert labels == ["Play", "Shuffle"]

    press = _press()
    await view.on_play(press)
    assert loaded == [[("One", "youtube"), ("Two", "spotify")]]
    started.assert_awaited_once()

    def reverse(items):
        items.reverse()

    monkeypatch.setattr("music.commands.playlist.random.shuffle", reverse)
    started.reset_mock()
    view = SavedPlaylist(saved, started)
    press = _press()
    await view.on_shuffle(press)
    assert loaded[-1] == [("Two", "spotify"), ("One", "youtube")]
    started.assert_awaited_once()

    played = []

    async def now(_member, _guild, track, platform):
        played.append((track.title, platform))
        return f"Playing {track.title}."

    monkeypatch.setattr("music.commands.playlist.play_now", now)
    started.reset_mock()
    view = SavedPlaylist(saved, started)
    view.one._values = ["1"]
    press = _press()
    await view.on_one(press)
    assert played == [("Two", "spotify")]
    started.assert_awaited_once()

    async def refused(_member, _guild, tracks):
        return "Join the voice channel first."

    monkeypatch.setattr("music.commands.playlist.load_line", refused)
    started.reset_mock()
    view = SavedPlaylist(saved, started)
    press = _press()
    await view.on_play(press)
    started.assert_not_awaited()
    assert view.is_finished() is False
    assert "Join the voice channel first" in press.send.await_args.args[0]

    async def open_some(_guild, songs):
        return Opened([(_named(songs[0].title), songs[0].platform)], ["Two"])

    monkeypatch.setattr("music.commands.playlist.open_recordings", open_some)
    monkeypatch.setattr("music.commands.playlist.load_line", start)
    started.reset_mock()
    view = SavedPlaylist(saved, started)
    press = _press()
    await view.on_play(press)
    started.assert_awaited_once()
    assert press.send.await_args.args[0] == "**>**  Playing One. Skipped Two"


async def test_playlist_shows_the_line_or_a_saved_list(monkeypatch, tmp_path):
    assert OsaMusify(MagicMock()).guild_config is guild_config
    store = GuildConfig(tmp_path / "guild.json")
    cog = OsaMusify(MagicMock(), guild_config=store)
    monkeypatch.setattr(MusicPlayer, "follow", lambda self, message: None)

    interaction = _ask()
    interaction.guild.voice_client = None
    interaction.response.send_message = AsyncMock()
    await cog.playlist.callback(cog, interaction, None)
    assert "Nothing is playing" in interaction.send.await_args.args[0]

    monkeypatch.setattr("cogs.osa_musify.queued", lambda guild: [])
    interaction = _ask()
    interaction.response.send_message = AsyncMock()
    await cog.playlist.callback(cog, interaction, None)
    assert "Nothing is after this song" in interaction.send.await_args.args[0]
    monkeypatch.undo()

    interaction = _ask()
    interaction.guild.id = 9300
    interaction.guild.voice_client = None
    interaction.response.send_message = AsyncMock()
    await cog.playlist.callback(cog, interaction, "Evening")
    assert "No playlist named Evening" in interaction.send.await_args.args[0]

    save_song(
        store, 9300, "Evening", SavedSong("One Wish", "youtube", "https://example.com/a"), 17,
    )
    interaction = _ask()
    interaction.guild.id = 9300
    interaction.response.send_message = AsyncMock()
    await cog.playlist.callback(cog, interaction, "evening")
    view = interaction.response.send_message.await_args.kwargs["view"]
    assert isinstance(view, SavedPlaylist)
    assert "**Evening**" in view.body.content
    assert "<@17>" in view.body.content

    guild, _member, _player, _tracks, note = await _line_up(9301, ["Alpha", "Beta"])
    try:
        assert note == "Playing Alpha."
        interaction = _ask()
        interaction.guild = guild
        interaction.response.send_message = AsyncMock()
        await cog.playlist.callback(cog, interaction, "   ")
        shown = interaction.response.send_message.await_args.kwargs["view"]
        assert isinstance(shown, LiveLine)
        assert "1. **Alpha**" in shown.body.content
        assert "2. Beta" in shown.body.content
    finally:
        await stop_track(guild)
