"""Play, the player, and playlists. No Discord login and no Lavalink node."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import discord

from cogs.osa_musify import OsaMusify
from guild import GuildConfig, guild_config
from music.panel import MARK_SEARCH, PANEL_SECONDS
from music.play.discover import EMPTY_SECONDS, GenreList
from music.play.search import MatchList
from music.player.card import MusicPlayer
from music.playlist.card import LiveLine
from music.lavalink import IDLE_SECONDS, Found
from music.playback import queue_tracks, stop_track, toggle_pause, _stored
from music.source import NAME_PLATFORM

from tests.music.support import _Player, _ask, _hearing, _named, _person, _playing, _press
from tests.music.play.support import _button
from tests.music.playlist.support import _line_up, _text

def test_commands_are_play_player_and_playlist():
    names = {command.name for command in OsaMusify.music.walk_commands()}
    assert names == {"play", "player", "playlist"}
    assert OsaMusify.music.name == "music"
    shown = next(command for command in OsaMusify.music.walk_commands() if command.name == "player")
    assert shown.description == "Show the music player"
    listed = next(command for command in OsaMusify.music.walk_commands() if command.name == "playlist")
    assert listed.description == "Show the queue, playlists, and songs already played"
    assert listed.parameters == []
    play = next(command for command in OsaMusify.music.walk_commands() if command.name == "play")
    assert play.description == "Play a song or a link. Leave it empty to pick a random song"
    assert [parameter.name for parameter in play.parameters] == ["search"]
    search = play.parameters[0]
    assert search.required is False
    assert search.description == "A song name or a link. Leave this empty to pick a random song"

async def test_a_kick_forgets_the_line_and_the_card(monkeypatch):
    guild, player, _track = await _playing()
    try:
        member = MagicMock()
        member.id = 18
        member.voice.channel = player.channel
        assert await queue_tracks(member, guild, [_named("Queued")]) == "Added Queued."
        guild.voice_client = None
        home = MagicMock()
        home.id = 20
        fresh = _Player(home)

        async def play(song):
            fresh._track = song

        fresh.play.side_effect = play
        home.connect = AsyncMock(return_value=fresh)
        member.voice.channel = home
        assert await queue_tracks(member, guild, [_named("Fresh"), _named("Second")]) == "Playing Fresh."
        assert _stored(guild).current.track.title == "Fresh"
        assert [song.track.title for song in _stored(guild).later] == ["Second"]

        cog = OsaMusify(MagicMock())
        cog.bot.user.id = 50
        card = MusicPlayer(guild)
        message = MagicMock()
        message.delete = AsyncMock()
        card.message = message
        cog._panels.append(card)
        leaver = MagicMock()
        leaver.id = 50
        leaver.guild = guild
        before = MagicMock()
        before.channel = home
        after = MagicMock()
        after.channel = None
        await cog.on_voice_state_update(leaver, before, after)
        assert _stored(guild) is None
        message.delete.assert_awaited_once()
        assert card._closed is True

        guild.voice_client = None
        gone = MusicPlayer(guild)
        press = _press(player)
        press.user.voice = None
        press.response.defer = AsyncMock()
        press.delete_original_response = AsyncMock()
        await gone.on_close(press)
        press.delete_original_response.assert_awaited_once()
        player.stop.assert_not_awaited()
        assert gone._closed is True

        async def no_wait(_seconds):
            return None

        monkeypatch.setattr("music.player.sync.asyncio.sleep", no_wait)
        guild.voice_client = player
        lingering = MusicPlayer(guild)
        lingering.message = message
        message.delete.reset_mock()
        guild.voice_client = None
        lingering.follow(message)
        await lingering._task
        message.delete.assert_awaited()
        assert lingering._closed is True
    finally:
        guild.voice_client = None


async def test_five_quiet_minutes_leave_and_close_the_player():
    guild, player, _track = await _playing()
    try:
        cog = OsaMusify(MagicMock())
        card = MusicPlayer(guild)
        message = MagicMock()
        message.delete = AsyncMock()
        card.message = message
        cog._panels.append(card)
        player._guild = guild

        await cog.on_wavelink_inactive_player(player)
        player.disconnect.assert_not_awaited()
        message.delete.assert_not_awaited()
        assert card._closed is False

        player._track = None
        _stored(guild).current = None
        await cog.on_wavelink_inactive_player(player)
        player.disconnect.assert_awaited_once()
        message.delete.assert_awaited_once()
        assert card._closed is True
    finally:
        guild.voice_client = None
        await stop_track(guild)


async def test_five_paused_minutes_leave_and_close_the_player(monkeypatch):
    guild, player, _track = await _playing()
    try:
        cog = OsaMusify(MagicMock())
        card = MusicPlayer(guild)
        message = MagicMock()
        message.delete = AsyncMock()
        card.message = message
        cog._panels.append(card)
        player._guild = guild
        seen: list[float] = []

        async def no_wait(seconds):
            seen.append(seconds)
            return None

        monkeypatch.setattr("music.playback.start._wait", no_wait)
        finished = asyncio.Event()

        def dispatch(event, target):
            assert event == "wavelink_inactive_player"
            assert target is player

            async def run():
                await cog.on_wavelink_inactive_player(target)
                finished.set()

            asyncio.create_task(run())

        player.client = MagicMock()
        player.client.dispatch = dispatch
        assert await toggle_pause(guild) is None
        await asyncio.wait_for(finished.wait(), timeout=1)
        assert seen == [IDLE_SECONDS]
        player.disconnect.assert_awaited_once()
        message.delete.assert_awaited_once()
        assert card._closed is True
    finally:
        guild.voice_client = None
        await stop_track(guild)


async def test_play_opens_genres_a_list_or_the_song(monkeypatch):
    cog = OsaMusify(MagicMock())
    monkeypatch.setattr(MusicPlayer, "follow", lambda self, message: None)

    async def blank(song):
        interaction = _ask()
        interaction.response.send_message = AsyncMock()
        interaction.response.is_done.return_value = False
        await cog.play.callback(cog, interaction, song)
        view = interaction.response.send_message.await_args.kwargs["view"]
        assert isinstance(view, GenreList)
        assert view.platform == "spotify"

    search = AsyncMock()
    monkeypatch.setattr("cogs.osa_musify.find_songs", search)
    await blank(None)
    await blank("   ")
    search.assert_not_awaited()

    interaction = _ask()
    interaction.user.id = 9
    interaction.response.is_done.return_value = True
    interaction.edit_original_response = AsyncMock()
    track = _named("One Wish", "Ray J")
    search.return_value = Found("one wish", "apple", [track], False)
    queued = AsyncMock()
    monkeypatch.setattr("cogs.osa_musify.queue_tracks", queued)
    await cog.play.callback(cog, interaction, "one wish")
    search.assert_awaited_once_with(interaction.guild, "one wish", NAME_PLATFORM)
    view = interaction.edit_original_response.await_args.kwargs["view"]
    assert isinstance(view, MatchList)
    assert view.platform == "apple"
    assert view.requester_id == 9
    assert view.tracks == [track]
    queued.assert_not_awaited()

    interaction = _ask()
    interaction.response.is_done.return_value = True
    interaction.edit_original_response = AsyncMock()
    interaction.original_response = AsyncMock()
    link = _named("Ocean Drive")
    search.return_value = Found("https://open.spotify.com/track/abc", "spotify", [link], True)
    queued.return_value = "Playing Ocean Drive."
    interaction.channel.send = AsyncMock(return_value=MagicMock())
    _hearing(interaction.guild)
    await cog.play.callback(cog, interaction, "https://open.spotify.com/track/abc")
    queued.assert_awaited_once()
    assert queued.await_args.args[2] == [link]
    assert queued.await_args.args[3] == "spotify"
    shown = interaction.channel.send.await_args.kwargs["view"]
    assert isinstance(shown, MusicPlayer)
    answered = interaction.edit_original_response.await_args.kwargs
    assert "Playing Ocean Drive" in answered["content"]
    assert answered["view"] is None

    interaction = _ask()
    interaction.response.is_done.return_value = True
    interaction.edit_original_response = AsyncMock()
    queued.reset_mock()
    queued.return_value = "Added Ocean Drive."
    panels = len(cog._panels)
    await cog.play.callback(cog, interaction, "https://open.spotify.com/track/abc")
    queued.assert_awaited_once()
    assert len(cog._panels) == panels
    interaction.delete_original_response.assert_awaited_once()
    interaction.edit_original_response.assert_not_awaited()

    interaction = _ask()
    interaction.response.is_done.return_value = True
    interaction.edit_original_response = AsyncMock()
    search.return_value = "Nothing on Apple Music matched that."
    await cog.play.callback(cog, interaction, "missing")
    empty = interaction.edit_original_response.await_args.kwargs["view"]
    assert isinstance(empty, MatchList)
    assert empty.tracks == []
    assert empty.platform == "apple"

    interaction = _ask()
    link = "https://music.apple.com/us/song/never-gonna-give-you-up/1"
    search.return_value = "Nothing on Apple Music matched that."
    await cog.play.callback(cog, interaction, link)
    assert "Nothing on Apple Music" in interaction.edit_original_response.await_args.kwargs["content"]

    interaction = _ask()
    search.return_value = "Apple Music did not answer."
    await cog.play.callback(cog, interaction, "missing")
    missed = interaction.edit_original_response.await_args.kwargs["view"]
    assert isinstance(missed, MatchList)
    assert missed.tracks == []
    assert missed.note == "Apple Music did not answer."

    interaction = _ask()
    interaction.response.is_done.return_value = True
    interaction.edit_original_response = AsyncMock()
    search.return_value = "Lavalink is not connected."
    await cog.play.callback(cog, interaction, "missing")
    assert "Lavalink is not connected" in interaction.edit_original_response.await_args.kwargs["content"]

async def test_opening_search_removes_the_empty_card_and_the_reverse():
    cog = OsaMusify(MagicMock())
    genre_message = MagicMock()
    genre_message.delete = AsyncMock()
    search_message = MagicMock()
    search_message.delete = AsyncMock()

    opening = _person(9)
    opening.response.send_message = AsyncMock()
    opening.response.is_done.return_value = False
    opening.original_response = AsyncMock(return_value=genre_message)
    await cog.play.callback(cog, opening, None)
    genre = opening.response.send_message.await_args.kwargs["view"]
    assert isinstance(genre, GenreList)
    assert genre.timeout == EMPTY_SECONDS

    other = _person(10)
    other.response.is_done.return_value = True
    other.original_response = AsyncMock(return_value=search_message)
    await cog._show_matches(other, "one", "apple", [])
    genre_message.delete.assert_not_awaited()
    assert genre.is_finished() is False
    assert cog._browse[(31, 10)].message is search_message

    searching = _person(9)
    searching.response.is_done.return_value = True
    searching.original_response = AsyncMock(return_value=search_message)
    await cog._show_matches(searching, "one", "apple", [])
    genre_message.delete.assert_awaited_once()
    assert genre.is_finished() is True
    listed = searching.edit_original_response.await_args.kwargs["view"]
    assert isinstance(listed, MatchList)
    assert listed.timeout == PANEL_SECONDS
    assert listed.message is search_message

    again = _person(9)
    again.response.send_message = AsyncMock()
    again.response.is_done.return_value = False
    again.original_response = AsyncMock(return_value=genre_message)
    genre_message.delete.reset_mock()
    await cog.play.callback(cog, again, None)
    search_message.delete.assert_awaited_once()
    assert listed.is_finished() is True

async def test_the_empty_card_search_opens_the_song_list(monkeypatch):
    cog = OsaMusify(MagicMock())
    opening = _person(9)
    opening.response.send_message = AsyncMock()
    opening.response.is_done.return_value = False
    message = MagicMock()
    message.delete = AsyncMock()
    opening.original_response = AsyncMock(return_value=message)
    await cog.play.callback(cog, opening, None)
    genre = opening.response.send_message.await_args.kwargs["view"]
    assert isinstance(genre, GenreList)
    heading = genre.children[0].children[0]
    assert isinstance(heading, discord.ui.Section)
    assert heading.children[0].content == "## Osaka Play"
    assert heading.accessory.label == MARK_SEARCH

    busy = _ask()
    busy.response.send_modal = AsyncMock()
    genre._working = True
    await heading.accessory.callback(busy)
    assert "Already working" in busy.send.await_args.args[0]
    busy.response.send_modal.assert_not_awaited()
    genre._working = False

    press = _ask()
    press.response.send_modal = AsyncMock()
    await heading.accessory.callback(press)
    modal = press.response.send_modal.await_args.args[0]
    assert modal.title == "Search"
    assert modal.query.component.default is None
    assert genre.timeout == PANEL_SECONDS

    track = _named("One Wish", "Ray J")
    found = AsyncMock(return_value=Found("one wish", "apple", [track], False))
    monkeypatch.setattr("cogs.osa_musify.find_songs", found)
    submit = _person(9)
    submit.response.is_done.return_value = True
    submit.original_response = AsyncMock(return_value=message)
    modal.query.component._value = "one wish"
    await modal.on_submit(submit)
    found.assert_awaited_once_with(submit.guild, "one wish", "apple")
    assert genre.is_finished() is True
    message.delete.assert_not_awaited()
    listed = submit.edit_original_response.await_args.kwargs["view"]
    assert isinstance(listed, MatchList)
    assert listed.query == "one wish"
    assert listed.platform == "apple"
    assert listed.tracks == [track]
    assert listed.requester_id == 9
    assert listed.message is message
    assert cog._browse[(31, 9)] is listed
    assert listed.children[0].children[0].accessory.label == MARK_SEARCH

async def test_a_sent_card_keeps_its_message():
    cog = OsaMusify(MagicMock())
    interaction = _ask()
    interaction.response.send_message = AsyncMock()
    interaction.response.is_done.return_value = False
    await cog.play.callback(cog, interaction, None)
    view = interaction.response.send_message.await_args.kwargs["view"]
    assert isinstance(view, GenreList)
    assert view.message is interaction.original_response.return_value

    interaction = _ask()
    interaction.response.is_done.return_value = True
    await cog._show_matches(interaction, "one", "apple", [])
    listed = interaction.edit_original_response.await_args.kwargs["view"]
    assert isinstance(listed, MatchList)
    assert listed.message is interaction.original_response.return_value

    interaction = _ask()
    interaction.guild.voice_client = None
    interaction.response.send_message = AsyncMock()
    shown = LiveLine(interaction.guild, MagicMock())
    await cog._show_list(interaction, shown)
    assert shown.message is interaction.original_response.return_value
    shown.stop()

async def test_a_playlist_search_reposts_the_play_card(monkeypatch):
    track = _named("Ocean Drive")
    monkeypatch.setattr(
        "cogs.osa_musify.find_songs",
        AsyncMock(return_value=Found("ocean drive", "apple", [track], False)),
    )
    cog = OsaMusify(MagicMock())
    old = MagicMock()
    old.id = 4
    old.delete = AsyncMock()
    play = MatchList("old", "apple", [_named("Old")], AsyncMock(), 9)
    play.message = old
    cog._browse[(31, 9)] = play

    playlist = MagicMock()
    playlist.id = 8
    playlist.delete = AsyncMock()
    sent = MagicMock()
    sent.id = 9
    press = _person(9)
    press.original_response = AsyncMock(return_value=playlist)
    press.followup.send = AsyncMock(return_value=sent)
    await cog._search(press, "ocean drive", fresh=True)

    assert play.is_finished() is True
    old.delete.assert_awaited_once()
    playlist.delete.assert_not_awaited()
    press.edit_original_response.assert_not_awaited()
    shown = press.followup.send.await_args.kwargs["view"]
    assert isinstance(shown, MatchList)
    assert shown.query == "ocean drive"
    assert shown.message is sent
    assert cog._browse[(31, 9)] is shown
    shown.stop()

async def test_a_link_from_the_playlist_leaves_that_card(monkeypatch):
    """Searching a link on the playlist card must not delete or replace it."""
    link = _named("Ocean Drive")
    monkeypatch.setattr(
        "cogs.osa_musify.find_songs",
        AsyncMock(return_value=Found("https://open.spotify.com/track/abc", "spotify", [link], True)),
    )
    queued = AsyncMock(return_value="Added Ocean Drive.")
    monkeypatch.setattr("cogs.osa_musify.queue_tracks", queued)
    monkeypatch.setattr(MusicPlayer, "follow", lambda self, message: None)
    cog = OsaMusify(MagicMock())
    play_message = MagicMock()
    play_message.id = 4
    play_message.delete = AsyncMock()
    play = MatchList("old", "apple", [_named("Old")], AsyncMock(), 9)
    play.message = play_message
    cog._browse[(31, 9)] = play

    press = _person(9)
    press.response.is_done.return_value = True
    press.followup.send = AsyncMock()
    press.channel.send = AsyncMock()
    await cog._search(press, "https://open.spotify.com/track/abc", fresh=True)
    press.delete_original_response.assert_awaited_once()
    press.edit_original_response.assert_not_awaited()
    press.channel.send.assert_not_awaited()
    press.send.assert_not_awaited()
    play_message.delete.assert_not_awaited()
    assert play.is_finished() is False
    assert cog._browse[(31, 9)] is play

    queued.return_value = "Join a voice channel first."
    press = _person(9)
    press.response.is_done.return_value = True
    press.channel.send = AsyncMock()
    await cog._search(press, "https://open.spotify.com/track/abc", fresh=True)
    assert "voice channel" in press.send.await_args.args[0]
    play_message.delete.assert_not_awaited()
    assert play.is_finished() is False

    queued.return_value = "Playing Ocean Drive."
    press = _person(9)
    _hearing(press.guild)
    press.response.is_done.return_value = True
    sent = MagicMock()
    press.channel.send = AsyncMock(return_value=sent)
    await cog._search(press, "https://open.spotify.com/track/abc", fresh=True)
    press.delete_original_response.assert_not_awaited()
    press.edit_original_response.assert_not_awaited()
    assert isinstance(press.channel.send.await_args.kwargs["view"], MusicPlayer)
    assert "Playing Ocean Drive" in press.send.await_args.args[0]
    play_message.delete.assert_awaited_once()
    assert play.is_finished() is True
    for panel in cog._panels:
        panel.shutdown()

async def test_search_again_keeps_the_platform(monkeypatch):
    calls = []

    async def search(guild, song, platform="youtube", mode=None):
        calls.append((song, platform, mode))
        return Found(song, platform, [_named("Ocean Drive")], False)

    monkeypatch.setattr("cogs.osa_musify.find_songs", search)
    cog = OsaMusify(MagicMock())
    message = MagicMock()
    message.delete = AsyncMock()
    view = MatchList(
        "one wish",
        "spotify",
        [_named("One Wish")],
        AsyncMock(),
        9,
        on_search=cog._search,
    )
    view.message = message
    cog._browse[(31, 9)] = view

    press = _ask()
    press.response.send_modal = AsyncMock()
    await _button(view, MARK_SEARCH).callback(press)
    modal = press.response.send_modal.await_args.args[0]
    assert modal.title == "Search again"
    assert modal.query.component.default == "one wish"

    blank = _ask()
    modal.query.component._value = "   "
    await modal.on_submit(blank)
    assert view.is_finished() is False
    assert "Name an artist or a song" in blank.send.await_args.args[0]
    assert calls == []

    submit = _person(9)
    submit.response.is_done.return_value = True
    submit.original_response = AsyncMock(return_value=message)
    modal.query.component._value = "ocean drive"
    await modal.on_submit(submit)
    assert calls == [("ocean drive", "spotify", None)]
    assert view.is_finished() is True
    assert view._closed is True
    message.delete.assert_not_awaited()
    listed = submit.edit_original_response.await_args.kwargs["view"]
    assert isinstance(listed, MatchList)
    assert listed is not view
    assert listed.platform == "spotify"
    assert listed.query == "ocean drive"
    assert cog._browse[(31, 9)] is listed

async def test_a_failed_search_keeps_the_card(monkeypatch):
    async def search(guild, song, platform="youtube", mode=None):
        return "Nothing on YouTube matched that."

    monkeypatch.setattr("cogs.osa_musify.find_songs", search)
    cog = OsaMusify(MagicMock())
    message = MagicMock()
    message.id = 77
    message.delete = AsyncMock()
    view = MatchList(
        "one wish",
        "spotify",
        [_named("One Wish")],
        AsyncMock(),
        9,
        on_search=cog._search,
    )
    view.message = message
    cog._browse[(31, 9)] = view
    press = _ask()
    press.response.send_modal = AsyncMock()
    await _button(view, MARK_SEARCH).callback(press)
    modal = press.response.send_modal.await_args.args[0]

    submit = _person(9)
    submit.response.is_done.return_value = True
    submit.original_response = AsyncMock(return_value=message)
    modal.query.component._value = "https://www.youtube.com/watch?v=abc"
    await modal.on_submit(submit)
    assert view.is_finished() is False
    assert view._closed is False
    assert view.tracks[0].title == "One Wish"
    message.delete.assert_not_awaited()
    assert "Nothing on YouTube" in submit.send.await_args.args[0]
    assert submit.edit_original_response.await_args.kwargs["view"] is view
    assert cog._browse[(31, 9)] is view

async def test_a_missed_name_still_opens_the_list(monkeypatch):
    async def search(guild, song, platform="youtube", mode=None):
        return "Nothing on Spotify matched that."

    monkeypatch.setattr("cogs.osa_musify.find_songs", search)
    cog = OsaMusify(MagicMock())
    message = MagicMock()
    message.id = 78
    message.delete = AsyncMock()
    view = MatchList(
        "one wish",
        "spotify",
        [_named("One Wish")],
        AsyncMock(),
        9,
        on_search=cog._search,
    )
    view.message = message
    cog._browse[(31, 9)] = view
    press = _ask()
    press.response.send_modal = AsyncMock()
    await _button(view, MARK_SEARCH).callback(press)
    modal = press.response.send_modal.await_args.args[0]
    submit = _person(9)
    submit.response.is_done.return_value = True
    submit.original_response = AsyncMock(return_value=message)
    modal.query.component._value = "ocean"
    await modal.on_submit(submit)
    assert view.is_finished() is True
    message.delete.assert_not_awaited()
    listed = submit.edit_original_response.await_args.kwargs["view"]
    assert isinstance(listed, MatchList)
    assert listed.tracks == []
    assert listed.platform == "spotify"
    assert listed.note == "Nothing on Spotify matched that."
    assert cog._browse[(31, 9)] is listed

async def test_a_link_that_does_not_start_keeps_the_card(monkeypatch):
    track = _named("Ocean Drive")
    monkeypatch.setattr(
        "cogs.osa_musify.find_songs",
        AsyncMock(return_value=Found(
            "https://www.youtube.com/watch?v=abc", "youtube", [track], True,
        )),
    )
    monkeypatch.setattr(
        "cogs.osa_musify.queue_tracks",
        AsyncMock(return_value="Join a voice channel first."),
    )
    cog = OsaMusify(MagicMock())
    message = MagicMock()
    message.delete = AsyncMock()
    view = MatchList(
        "one wish",
        "apple",
        [_named("One Wish")],
        AsyncMock(),
        9,
        on_search=cog._search,
    )
    view.message = message
    press = _ask()
    press.response.send_modal = AsyncMock()
    await _button(view, MARK_SEARCH).callback(press)
    modal = press.response.send_modal.await_args.args[0]
    submit = _person(9)
    submit.response.is_done.return_value = True
    submit.original_response = AsyncMock(return_value=message)
    modal.query.component._value = "https://www.youtube.com/watch?v=abc"
    await modal.on_submit(submit)
    assert view.is_finished() is False
    assert "Join a voice channel first" in submit.send.await_args.args[0]
    assert submit.edit_original_response.await_args.kwargs["view"] is view
    message.delete.assert_not_awaited()

async def test_a_closed_card_does_not_search_again(monkeypatch):
    search = AsyncMock()
    monkeypatch.setattr("cogs.osa_musify.find_songs", search)
    cog = OsaMusify(MagicMock())
    view = MatchList(
        "one wish",
        "apple",
        [_named("One Wish")],
        AsyncMock(),
        9,
        on_search=cog._search,
    )
    press = _ask()
    press.response.send_modal = AsyncMock()
    await _button(view, MARK_SEARCH).callback(press)
    modal = press.response.send_modal.await_args.args[0]
    view.stop()
    submit = _ask()
    modal.query.component._value = "ocean"
    await modal.on_submit(submit)
    search.assert_not_awaited()
    assert view.is_finished() is True
    assert "That card has closed" in submit.send.await_args.args[0]
    submit.edit_original_response.assert_not_awaited()

async def test_search_again_keeps_the_platform_menu():
    """Stopping the old card must not unregister the new card's platform menu."""
    from discord.ui.view import ViewStore

    state = MagicMock()
    store = ViewStore(state)
    state.store_view = store.add_view
    message = MagicMock()
    message.id = 42
    message._state = state
    old = MatchList("one", "apple", [_named("One")], AsyncMock(), 9)
    new = MatchList("two", "apple", [_named("Two")], AsyncMock(), 9)
    old.message = message
    new.message = message
    store.add_view(old, message.id)
    store.add_view(new, message.id)
    cog = OsaMusify(MagicMock())
    cog._browse[(31, 9)] = old
    try:
        await cog._keep_one(cog._browse, new, (31, 9))
        menu = new.platforms
        assert store._views[message.id][(menu.type.value, menu.custom_id)] is menu
        assert old.is_finished() is True
        assert new.is_finished() is False
        assert cog._browse[(31, 9)] is new
    finally:
        if not old.is_finished():
            old.stop()
        new.stop()

async def test_two_player_opens_leave_one_card(monkeypatch):
    monkeypatch.setattr(MusicPlayer, "follow", lambda self, message: None)
    cog = OsaMusify(MagicMock())
    guild = MagicMock()
    guild.id = 31
    _hearing(guild)
    started = asyncio.Event()
    release = asyncio.Event()
    opened: list[MusicPlayer] = []

    async def send(*_args, **kwargs):
        opened.append(kwargs["view"])
        if len(opened) == 1:
            started.set()
            await release.wait()
        message = MagicMock()
        message.delete = AsyncMock()
        return message

    first = _ask()
    first.guild = guild
    first.response.is_done.return_value = True
    first.channel.send = AsyncMock(side_effect=send)
    second = _ask()
    second.guild = guild
    second.response.is_done.return_value = True
    second.channel.send = AsyncMock(side_effect=send)
    pending = asyncio.create_task(cog._show_player(first))
    await started.wait()
    other = asyncio.create_task(cog._show_player(second))
    await asyncio.sleep(0)
    assert len(opened) == 1
    release.set()
    await pending
    await other
    assert len(cog._panels) == 1
    assert cog._panels[0] is opened[1]
    assert opened[0]._closed is True
    cog._panels[0].shutdown()

async def test_a_failed_player_leaves_the_play_card(monkeypatch):
    cog = OsaMusify(MagicMock())
    press = _person(9)
    press.response.send_message = AsyncMock()
    press.response.is_done.return_value = False
    press.original_response = AsyncMock(return_value=MagicMock())
    await cog.play.callback(cog, press, None)
    view = press.response.send_message.await_args.kwargs["view"]
    assert isinstance(view, GenreList)

    failed = _person(9)
    _hearing(failed.guild)
    failed.response.is_done.return_value = True
    failed.channel.send = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=500), "no"))
    await view.on_started(failed)
    failed.edit_original_response.assert_not_awaited()
    assert "opening the player" in failed.send.await_args.args[0]
    view.stop()

async def test_a_failed_player_send_keeps_the_open_one(monkeypatch):
    monkeypatch.setattr(MusicPlayer, "follow", lambda self, message: None)
    cog = OsaMusify(MagicMock())
    guild = MagicMock()
    guild.id = 31
    _hearing(guild)
    old = MusicPlayer(guild, discard=cog._forget_panel)
    old.message = MagicMock()
    old.message.delete = AsyncMock()
    cog._panels.append(old)
    press = _ask()
    press.guild = guild
    press.response.is_done.return_value = True
    press.channel.send = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=500), "no"))
    await cog._show_player(press)
    assert old._closed is False
    old.message.delete.assert_not_awaited()
    assert cog._panels == [old]

    press = _ask()
    press.guild = guild
    press.response.is_done.return_value = True
    sent = MagicMock()
    press.channel.send = AsyncMock(return_value=sent)
    await cog._show_player(press)
    assert old._closed is True
    old.message.delete.assert_awaited_once()
    assert len(cog._panels) == 1
    assert cog._panels[0] is not old
    cog._panels[0].shutdown()

async def test_the_cross_keeps_the_next_song_from_posting_the_player(monkeypatch):
    monkeypatch.setattr(MusicPlayer, "follow", lambda self, message: None)
    guild, _player, _track = await _playing()
    cog = OsaMusify(MagicMock())
    try:
        press = _ask()
        press.guild = guild
        press.response.is_done.return_value = True
        sent = MagicMock()
        sent.delete = AsyncMock()
        press.channel.send = AsyncMock(return_value=sent)
        await cog._show_player(press)
        card = cog._panels[0]
        closer = _ask()
        closer.guild = guild
        closer.response.defer = AsyncMock()
        closer.response.is_done.return_value = False
        closer.delete_original_response = AsyncMock()
        await card.on_close(closer)
        closer.delete_original_response.assert_awaited()
        assert card._closed is True
        assert cog._panels == []

        again = _ask()
        again.guild = guild
        again.response.is_done.return_value = True
        again.channel.send = AsyncMock()
        await cog._open_beside(again)
        await cog._open_from_card(again)
        again.channel.send.assert_not_awaited()
        assert cog._panels == []

        shown = _ask()
        shown.guild = guild
        shown.response.is_done.return_value = False
        shown.response.send_message = AsyncMock()
        posted = MagicMock()
        posted.delete = AsyncMock()
        shown.original_response = AsyncMock(return_value=posted)
        await cog.player.callback(cog, shown)
        assert isinstance(shown.response.send_message.await_args.kwargs["view"], MusicPlayer)
        assert len(cog._panels) == 1

        later = _ask()
        later.guild = guild
        later.response.is_done.return_value = True
        later.channel.send = AsyncMock(return_value=MagicMock())
        await cog._show_player(later)
        later.channel.send.assert_awaited()
    finally:
        for panel in list(cog._panels):
            panel.shutdown()
        await stop_track(guild)

async def test_the_player_command_stays_down_when_the_song_is_over():
    guild, player, _track = await _playing()
    cog = OsaMusify(MagicMock())
    try:
        player._track = None
        press = _ask()
        press.guild = guild
        press.response.is_done.return_value = False
        press.send = AsyncMock()
        await cog.player.callback(cog, press)
        assert "Nothing is playing" in press.send.await_args.args[0]
        assert cog._panels == []
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)


async def test_one_public_playlist_card_per_server():
    cog = OsaMusify(MagicMock())
    first_message = MagicMock()
    first_message.id = 1
    first_message.delete = AsyncMock()
    other_message = MagicMock()
    other_message.id = 2
    other_message.delete = AsyncMock()
    second_message = MagicMock()
    second_message.id = 3
    second_message.delete = AsyncMock()
    elsewhere_message = MagicMock()
    elsewhere_message.id = 4
    elsewhere_message.delete = AsyncMock()
    first = _ask()
    first.guild.id = 31
    first.user.id = 7
    first.response.send_message = AsyncMock()
    first.response.is_done.return_value = False
    first.original_response = AsyncMock(return_value=first_message)
    await cog.playlist.callback(cog, first)
    opened = first.response.send_message.await_args.kwargs["view"]
    assert "ephemeral" not in first.response.send_message.await_args.kwargs

    other = _ask()
    other.guild.id = 31
    other.user.id = 8
    other.response.send_message = AsyncMock()
    other.response.is_done.return_value = False
    other.original_response = AsyncMock(return_value=other_message)
    await cog.playlist.callback(cog, other)
    first_message.delete.assert_awaited_once()
    assert opened.is_finished() is True
    theirs = other.response.send_message.await_args.kwargs["view"]
    assert cog._lines[31] is theirs

    elsewhere = _ask()
    elsewhere.guild.id = 44
    elsewhere.user.id = 7
    elsewhere.response.send_message = AsyncMock()
    elsewhere.response.is_done.return_value = False
    elsewhere.original_response = AsyncMock(return_value=elsewhere_message)
    await cog.playlist.callback(cog, elsewhere)
    other_message.delete.assert_not_awaited()
    assert theirs.is_finished() is False
    other_server = elsewhere.response.send_message.await_args.kwargs["view"]
    assert cog._lines[44] is other_server
    assert cog._lines[31] is theirs

    second = _ask()
    second.guild.id = 31
    second.user.id = 7
    second.response.send_message = AsyncMock()
    second.response.is_done.return_value = False
    second.original_response = AsyncMock(return_value=second_message)
    await cog.playlist.callback(cog, second)
    other_message.delete.assert_awaited_once()
    elsewhere_message.delete.assert_not_awaited()
    assert theirs.is_finished() is True
    assert other_server.is_finished() is False
    again = second.response.send_message.await_args.kwargs["view"]
    assert "ephemeral" not in second.response.send_message.await_args.kwargs
    assert cog._lines[31] is again
    assert cog._lines[44] is other_server
    again.stop()
    other_server.stop()


async def test_a_failed_playlist_send_keeps_the_open_one():
    cog = OsaMusify(MagicMock())
    first_message = MagicMock()
    first_message.id = 1
    first_message.delete = AsyncMock()
    first = _ask()
    first.guild.id = 31
    first.response.send_message = AsyncMock()
    first.response.is_done.return_value = False
    first.original_response = AsyncMock(return_value=first_message)
    await cog.playlist.callback(cog, first)
    opened = first.response.send_message.await_args.kwargs["view"]

    failed = _ask()
    failed.guild.id = 31
    failed.response.send_message = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(status=500), "no"),
    )
    failed.response.is_done.return_value = True
    await cog.playlist.callback(cog, failed)
    first_message.delete.assert_not_awaited()
    assert opened.is_finished() is False
    assert cog._lines[31] is opened
    opened.stop()

async def test_playlist_shows_the_line(monkeypatch, tmp_path):
    assert OsaMusify(MagicMock()).guild_config is guild_config
    store = GuildConfig(tmp_path / "guild.json")
    cog = OsaMusify(MagicMock(), guild_config=store)
    monkeypatch.setattr(MusicPlayer, "follow", lambda self, message: None)

    interaction = _ask()
    interaction.guild.voice_client = None
    interaction.response.send_message = AsyncMock()
    await cog.playlist.callback(cog, interaction)
    shown = interaction.response.send_message.await_args.kwargs["view"]
    assert isinstance(shown, LiveLine)
    assert "Nothing is playing" in _text(shown)
    assert "Use /music play to play a song" in _text(shown)
    shown.stop()

    guild, _member, _player, _tracks, note = await _line_up(9301, ["Alpha", "Beta"])
    shown = None
    try:
        assert note == "Playing Alpha."
        interaction = _ask()
        interaction.guild = guild
        interaction.response.send_message = AsyncMock()
        await cog.playlist.callback(cog, interaction)
        shown = interaction.response.send_message.await_args.kwargs["view"]
        assert isinstance(shown, LiveLine)
        assert "### Now Playing" in _text(shown)
        assert "Alpha" in _text(shown)
        assert "Beta" in _text(shown)
    finally:
        if shown is not None:
            shown.stop()
        await stop_track(guild)
