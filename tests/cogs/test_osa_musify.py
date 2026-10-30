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
from music.playlist.card import PlaylistCard
from music.lavalink import IDLE_SECONDS, Found
from music.playback import advance_from_end, now_playing, queue_tracks, stop_track, _stored
from music.player.sync import REFRESH_SECONDS
from music.source import NAME_SEARCH_PLATFORM

from tests.music.support import _Player, _ask, _hearing, _named, _person, _playing, _press
from tests.music.play.support import _button
from tests.music.playlist.support import _line_up, _text


_real_sleep = asyncio.sleep


async def _hold(seconds: float) -> None:
    """The player tick waits five seconds. Every other sleep still runs."""
    if seconds == REFRESH_SECONDS:
        await asyncio.Event().wait()
        return
    await _real_sleep(seconds)


async def _settle() -> None:
    """Joins and card refreshes that the command started."""
    names = {"music-idle", "music-join", "music-line", "music-warm"}
    for _ in range(8):
        pending = [
            task
            for task in asyncio.all_tasks()
            if not task.done() and task.get_name() in names
        ]
        if not pending:
            return
        await asyncio.wait_for(asyncio.gather(*pending), timeout=2)


async def _finish(cog: OsaMusify) -> None:
    for panel in list(cog._panels):
        panel.shutdown()
    for view in list(cog._playlist_cards.values()):
        if not view.is_finished():
            view.stop()
    for view in list(cog._browse.values()):
        if not view.is_finished():
            view.stop()
    await asyncio.sleep(0)


async def _song_ends(guild, player) -> None:
    player._guild = guild
    player._last_position = 148_000
    await advance_from_end(type("End", (), {"reason": "finished", "player": player})())
    await _settle()


async def _summon_player(cog, guild, player):
    """/music player, the way someone in the room opens the card."""
    press = _press(player)
    press.guild = guild
    press.response.is_done.return_value = False
    message = MagicMock()
    message.delete = AsyncMock()
    message.edit = AsyncMock()
    press.original_response = AsyncMock(return_value=message)
    press.response.send_message = AsyncMock()
    await cog.player.callback(cog, press)
    assert len(cog._panels) == 1
    assert isinstance(press.response.send_message.await_args.kwargs["view"], MusicPlayer)
    return message


def _join_as(guild, channel):
    """Discord sets the voice client when this channel accepts the bot."""
    player = _Player(channel)

    async def play(song):
        player._track = song
        player._guild = guild

    player.play.side_effect = play

    async def connect(**_kwargs):
        guild.voice_client = player
        return player

    channel.connect = connect
    return player


async def _play_link(monkeypatch, cog, guild, channel, title: str):
    """/music play with a link. A deferred slash command, then the channel card."""
    track = _named(title)
    track.uri = "https://open.spotify.com/track/abc"
    track.length = 180_000

    async def search(*_args, **_kwargs):
        return Found("https://open.spotify.com/track/abc", "spotify", [track], True)

    monkeypatch.setattr("cogs.osa_musify.find_songs", search)
    playing = getattr(guild, "voice_client", None)
    if playing is None:
        _join_as(guild, channel)
    else:
        async def play(song):
            playing._track = song

        playing.play.side_effect = play
    press = _ask()
    press.guild = guild
    press.user.id = 17
    press.user.voice.channel = channel
    press.response.is_done.return_value = True
    sent = MagicMock()
    sent.delete = AsyncMock()
    sent.edit = AsyncMock()
    press.channel = MagicMock()
    press.channel.send = AsyncMock(return_value=sent)
    await cog.play.callback(cog, press, "https://open.spotify.com/track/abc")
    await _settle()
    return press

def case_commands_are_play_player_and_playlist():
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

async def test_a_kick_removes_the_player_and_keeps_the_queue(monkeypatch):
    """The bot is removed from voice. The card goes. The queue is still there to open."""
    monkeypatch.setattr("music.player.sync.asyncio.sleep", _hold)
    guild, player, track = await _playing()
    cog = OsaMusify(MagicMock())
    cog.bot.user.id = 50
    try:
        waiting = _named("After")
        waiting.uri = "https://example.com/after"
        member = MagicMock()
        member.id = 18
        member.voice.channel = player.channel
        assert await queue_tracks(member, guild, [waiting]) == "Added After."
        message = await _summon_player(cog, guild, player)

        leaver = MagicMock()
        leaver.id = 50
        leaver.guild = guild
        before = MagicMock()
        before.channel = player.channel
        after = MagicMock()
        after.channel = None
        guild.voice_client = None
        await cog.on_voice_state_update(leaver, before, after)
        message.delete.assert_awaited()
        assert cog._panels == []
        assert cog._is_closed(guild) is False

        listing = _ask()
        listing.guild = guild
        listing.response.is_done.return_value = False
        await cog.playlist.callback(cog, listing)
        shown = listing.edit_original_response.await_args.kwargs["view"]
        text = _text(shown)
        assert track.title in text
        assert "After" in text
    finally:
        await _finish(cog)
        guild.voice_client = None


async def test_five_quiet_minutes_leave_and_the_next_song_posts_the_player(monkeypatch):
    """A song on stays. After it ends, five quiet minutes leave, and the next song posts a card."""
    monkeypatch.setattr("music.player.sync.asyncio.sleep", _hold)
    guild, player, _track = await _playing()
    cog = OsaMusify(MagicMock())
    try:
        message = await _summon_player(cog, guild, player)
        card = cog._panels[0]
        player._guild = guild
        await cog.on_wavelink_inactive_player(player)
        player.disconnect.assert_not_awaited()
        message.delete.assert_not_awaited()
        assert card._closed is False

        async def dropped():
            guild.voice_client = None

        player.disconnect.side_effect = dropped
        await _song_ends(guild, player)
        assert card._closed is True
        message.delete.assert_awaited()
        player.disconnect.assert_not_awaited()
        assert cog._is_closed(guild) is False

        await cog.on_wavelink_inactive_player(player)
        player.disconnect.assert_awaited()
        assert cog._panels == []
        assert cog._is_closed(guild) is False

        started = await _play_link(monkeypatch, cog, guild, player.channel, "Ocean Drive")
        assert _stored(guild).current.track.title == "Ocean Drive"
        assert "Playing Ocean Drive" in started.edit_original_response.await_args.kwargs["content"]
        posted = started.channel.send.await_args.kwargs["view"]
        assert isinstance(posted, MusicPlayer)
    finally:
        await _finish(cog)
        if guild.voice_client is not None:
            await stop_track(guild)


async def test_five_paused_minutes_leave_and_the_song_can_be_opened(monkeypatch):
    """Pausing for five minutes leaves and takes the card. Opening the player brings the song back."""
    monkeypatch.setattr("music.player.sync.asyncio.sleep", _hold)
    gate = asyncio.Event()
    seen: list[float] = []

    async def no_wait(seconds):
        seen.append(seconds)
        await gate.wait()

    monkeypatch.setattr("music.playback.start._wait", no_wait)
    guild, player, track = await _playing()
    cog = OsaMusify(MagicMock())
    try:
        message = await _summon_player(cog, guild, player)
        card = cog._panels[0]
        player._guild = guild
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

        async def dropped():
            guild.voice_client = None

        player.disconnect.side_effect = dropped
        press = _press(player)
        press.guild = guild
        press.response.is_done.return_value = False
        await card.pause.callback(press)
        await asyncio.sleep(0)
        assert seen == [IDLE_SECONDS]
        assert player.paused is True
        assert card._closed is False
        gate.set()
        await asyncio.wait_for(finished.wait(), timeout=2)
        message.delete.assert_awaited()
        player.disconnect.assert_awaited()
        assert card._closed is True
        assert cog._panels == []
        assert cog._is_closed(guild) is False
        assert now_playing(guild).title == track.title
        assert now_playing(guild).paused is True

        async def search(_guild, query, _platform):
            return Found(query, "youtube", [track], True)

        monkeypatch.setattr("music.playback.find_songs", search)

        async def connect(**_kwargs):
            guild.voice_client = player
            return player

        async def play(song, **_kwargs):
            player._track = song

        player.channel.connect = connect
        player.play.side_effect = play
        fresh_message = await _summon_player(cog, guild, player)
        fresh = cog._panels[0]
        assert track.title in fresh.song.content
        assert fresh.pause.style == discord.ButtonStyle.primary
        resume = _press(player)
        resume.guild = guild
        resume.response.is_done.return_value = False
        await fresh.pause.callback(resume)
        assert fresh._closed is False
        fresh_message.delete.assert_not_awaited()
        assert guild.voice_client is player
        assert now_playing(guild) is not None
        assert now_playing(guild).paused is False
        assert now_playing(guild).title == track.title
        assert fresh.pause.style == discord.ButtonStyle.secondary
    finally:
        await _finish(cog)
        if guild.voice_client is not None:
            await stop_track(guild)


async def test_play(monkeypatch):
    """The commands are play, player, and playlist. Play opens genres, a list, or the song, including an artist link, several songs, and a link while paused."""
    case_commands_are_play_player_and_playlist()
    with monkeypatch.context() as patch:
        await case_play_opens_genres_a_list_or_the_song(patch)
    with monkeypatch.context() as patch:
        await case_an_artist_link_opens_a_list(patch)
    with monkeypatch.context() as patch:
        await case_several_songs_stay_on_screen(patch)
    with monkeypatch.context() as patch:
        await case_a_link_while_paused_says_so_and_stays_out(patch)

async def case_play_opens_genres_a_list_or_the_song(monkeypatch):
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
    search.assert_awaited_once_with(interaction.guild, "one wish", NAME_SEARCH_PLATFORM)
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

async def case_an_artist_link_opens_a_list(monkeypatch):
    """An artist page is a list to pick from. It does not start the first songs."""
    monkeypatch.setattr("music.lavalink.node_is_ready", lambda: True)
    songs = [_named("One"), _named("Two")]
    monkeypatch.setattr("music.source.search_track", AsyncMock(return_value=songs))
    cog = OsaMusify(MagicMock())
    interaction = _ask()
    interaction.response.is_done.return_value = True
    interaction.edit_original_response = AsyncMock()
    _hearing(interaction.guild)
    queued = AsyncMock()
    monkeypatch.setattr("cogs.osa_musify.queue_tracks", queued)
    try:
        await cog.play.callback(cog, interaction, "https://open.spotify.com/artist/abc")
        queued.assert_not_awaited()
        view = interaction.edit_original_response.await_args.kwargs["view"]
        assert isinstance(view, MatchList)
        assert view.tracks == songs
    finally:
        await _finish(cog)


async def case_several_songs_stay_on_screen(monkeypatch):
    """An album add names the count. One song still stays quiet."""
    cog = OsaMusify(MagicMock())
    link = _named("Ocean Drive")

    async def search(*_args, **_kwargs):
        return Found("https://open.spotify.com/album/abc", "spotify", [link, link], True)

    monkeypatch.setattr("cogs.osa_musify.find_songs", search)
    queued = AsyncMock(return_value="Added 4 songs.")
    monkeypatch.setattr("cogs.osa_musify.queue_tracks", queued)
    try:
        interaction = _ask()
        interaction.response.is_done.return_value = True
        interaction.edit_original_response = AsyncMock()
        _hearing(interaction.guild)
        await cog.play.callback(cog, interaction, "https://open.spotify.com/album/abc")
        shown = interaction.edit_original_response.await_args.kwargs["content"]
        assert "Added 4 songs" in shown
        interaction.delete_original_response.assert_not_awaited()

        interaction = _ask()
        interaction.response.is_done.return_value = True
        interaction.edit_original_response = AsyncMock()
        _hearing(interaction.guild)
        queued.return_value = "Added 4 songs. The queue is full."
        await cog.play.callback(cog, interaction, "https://open.spotify.com/album/abc")
        shown = interaction.edit_original_response.await_args.kwargs["content"]
        assert "Added 4 songs" in shown
        assert "queue is full" in shown

        interaction = _ask()
        interaction.response.is_done.return_value = True
        interaction.edit_original_response = AsyncMock()
        _hearing(interaction.guild)
        queued.return_value = "Added Ocean Drive."
        await cog.play.callback(cog, interaction, "https://open.spotify.com/album/abc")
        interaction.delete_original_response.assert_awaited_once()
        interaction.edit_original_response.assert_not_awaited()
    finally:
        await _finish(cog)


async def case_a_link_while_paused_says_so_and_stays_out(monkeypatch):
    warmed = MagicMock()
    monkeypatch.setattr("cogs.osa_musify.paused_away", lambda guild: True)
    monkeypatch.setattr("cogs.osa_musify.warm_voice", warmed)
    monkeypatch.setattr(
        "cogs.osa_musify.find_songs",
        AsyncMock(return_value=Found("https://open.spotify.com/track/abc", "spotify", [_named("Ocean Drive")], True)),
    )
    monkeypatch.setattr(
        "cogs.osa_musify.queue_tracks",
        AsyncMock(return_value="Added Ocean Drive. I Ain't Worried is still paused."),
    )
    cog = OsaMusify(MagicMock())
    interaction = _ask()
    interaction.response.is_done.return_value = True
    interaction.edit_original_response = AsyncMock()
    interaction.guild.voice_client = None
    await cog.play.callback(cog, interaction, "https://open.spotify.com/track/abc")
    warmed.assert_not_called()
    interaction.delete_original_response.assert_not_awaited()
    interaction.channel.send.assert_not_called()
    shown = interaction.edit_original_response.await_args.kwargs["content"]
    assert "still paused" in shown
    assert "Ocean Drive" in shown

async def test_the_play_card_opens(monkeypatch):
    """Opening search replaces the empty card. Search on that card opens the list, and a sent card keeps its message."""
    await case_opening_search_removes_the_empty_card_and_the_reverse()
    with monkeypatch.context() as patch:
        await case_the_empty_card_search_opens_the_song_list(patch)
    await case_a_sent_card_keeps_its_message()

async def case_opening_search_removes_the_empty_card_and_the_reverse():
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

async def case_the_empty_card_search_opens_the_song_list(monkeypatch):
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
    busy.response.send_modal.assert_awaited()
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

async def case_a_sent_card_keeps_its_message():
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
    shown = PlaylistCard(interaction.guild, MagicMock())
    await cog._show_list(interaction, shown)
    assert shown.message is interaction.original_response.return_value
    shown.stop()

async def test_search_again_from_the_playlist(monkeypatch):
    """A playlist search reposts the play card. A link from the playlist leaves that card."""
    with monkeypatch.context() as patch:
        await case_a_playlist_search_reposts_the_play_card(patch)
    with monkeypatch.context() as patch:
        await case_a_link_from_the_playlist_leaves_that_card(patch)

async def case_a_playlist_search_reposts_the_play_card(monkeypatch):
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
    await cog._search(press, "ocean drive", new_message=True)

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

async def case_a_link_from_the_playlist_leaves_that_card(monkeypatch):
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
    await cog._search(press, "https://open.spotify.com/track/abc", new_message=True)
    press.delete_original_response.assert_awaited_once()
    press.edit_original_response.assert_not_awaited()
    # The song was only added. A card opened while that was decided does not stay.
    assert cog._panels == []
    if press.channel.send.await_count:
        assert press.channel.send.await_args.kwargs["view"]._closed is True
    press.send.assert_not_awaited()
    play_message.delete.assert_not_awaited()
    assert play.is_finished() is False
    assert cog._browse[(31, 9)] is play

    queued.return_value = "Join a voice channel first."
    press = _person(9)
    press.response.is_done.return_value = True
    press.channel.send = AsyncMock()
    await cog._search(press, "https://open.spotify.com/track/abc", new_message=True)
    assert "voice channel" in press.send.await_args.args[0]
    play_message.delete.assert_not_awaited()
    assert play.is_finished() is False

    queued.return_value = "Playing Ocean Drive."
    press = _person(9)
    _hearing(press.guild)
    press.response.is_done.return_value = True
    sent = MagicMock()
    press.channel.send = AsyncMock(return_value=sent)
    await cog._search(press, "https://open.spotify.com/track/abc", new_message=True)
    press.delete_original_response.assert_not_awaited()
    press.edit_original_response.assert_not_awaited()
    assert isinstance(press.channel.send.await_args.kwargs["view"], MusicPlayer)
    assert "Playing Ocean Drive" in press.send.await_args.args[0]
    play_message.delete.assert_awaited_once()
    assert play.is_finished() is True
    for panel in cog._panels:
        panel.shutdown()

async def test_search_again(monkeypatch):
    """Search again keeps the platform and can still switch it."""
    with monkeypatch.context() as patch:
        await case_search_again_keeps_the_platform(patch)
    with monkeypatch.context() as patch:
        await case_search_again_still_switches_platform(patch)

async def case_search_again_keeps_the_platform(monkeypatch):
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

async def test_a_search_that_does_not_replace_the_card(monkeypatch):
    """A failed search, a missed name, a link that does not start, and a closed card leave the right card up."""
    with monkeypatch.context() as patch:
        await case_a_failed_search_keeps_the_card(patch)
    with monkeypatch.context() as patch:
        await case_a_missed_name_still_opens_the_list(patch)
    with monkeypatch.context() as patch:
        await case_a_link_that_does_not_start_keeps_the_card(patch)
    with monkeypatch.context() as patch:
        await case_a_closed_card_does_not_search_again(patch)

async def case_a_failed_search_keeps_the_card(monkeypatch):
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

async def case_a_missed_name_still_opens_the_list(monkeypatch):
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

async def case_a_link_that_does_not_start_keeps_the_card(monkeypatch):
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

async def case_a_closed_card_does_not_search_again(monkeypatch):
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

async def case_search_again_still_switches_platform(monkeypatch):
    """Search again replaces the card on the same message. The platform menu still receives the click."""
    from discord.ui.view import ViewStore

    calls = []

    async def search(_guild, song, platform="youtube", mode=None):
        calls.append((song, platform, mode))
        title = "Spot" if platform == "spotify" else "Wish"
        return Found(song, platform, [_named(title)], False)

    monkeypatch.setattr("cogs.osa_musify.find_songs", search)
    monkeypatch.setattr("music.play.search.find_songs", search)
    state = MagicMock()
    store = ViewStore(state)
    state.store_view = store.add_view
    message = MagicMock()
    message.id = 42
    message.delete = AsyncMock()
    message._state = state
    cog = OsaMusify(MagicMock())
    try:
        opening = _person(9)
        opening.response.is_done.return_value = True
        opening.original_response = AsyncMock(return_value=message)
        await cog.play.callback(cog, opening, "one wish")
        listed = opening.edit_original_response.await_args.kwargs["view"]
        assert isinstance(listed, MatchList)
        store.add_view(listed, message.id)

        press = _ask()
        press.response.send_modal = AsyncMock()
        await _button(listed, MARK_SEARCH).callback(press)
        modal = press.response.send_modal.await_args.args[0]
        submit = _person(9)
        submit.response.is_done.return_value = True
        submit.original_response = AsyncMock(return_value=message)
        modal.query.component._value = "ocean"
        await modal.on_submit(submit)
        assert listed.is_finished() is True

        click = _person(9)
        click.message = message
        click.data = {"values": ["spotify"]}

        async def defer(*_args, **_kwargs):
            click.response.is_done.return_value = True

        click.response.defer.side_effect = defer
        before = {task for task in asyncio.all_tasks()}
        store.dispatch_view(discord.ComponentType.string_select.value, "music-platform", click)
        started = [
            task
            for task in asyncio.all_tasks()
            if task not in before and task.get_name().startswith("discord-ui-view-dispatch")
        ]
        assert started, "the platform menu was not on the message"
        await asyncio.wait_for(asyncio.gather(*started), timeout=2)
        shown = cog._browse[(31, 9)]
        assert shown is not listed
        assert shown.is_finished() is False
        assert shown.platform == "spotify"
        assert shown.query == "ocean"
        assert shown.tracks[0].title == "Spot"
        assert ("ocean", "spotify", None) in calls
    finally:
        await _finish(cog)

async def test_the_player_command(monkeypatch):
    """Two opens leave one card. A failed open or a failed send leaves the card that is up. Nothing playing posts no card."""
    with monkeypatch.context() as patch:
        await case_two_player_opens_leave_one_card(patch)
    with monkeypatch.context() as patch:
        await case_a_failed_player_leaves_the_play_card(patch)
    with monkeypatch.context() as patch:
        await case_a_failed_player_send_keeps_the_open_one(patch)
    await case_the_player_command_stays_down_when_the_song_is_over()

async def case_two_player_opens_leave_one_card(monkeypatch):
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

async def case_a_failed_player_leaves_the_play_card(monkeypatch):
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

async def case_a_failed_player_send_keeps_the_open_one(monkeypatch):
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
    """Someone in the room closes the card. The next song starts without one until they ask."""
    monkeypatch.setattr("music.player.sync.asyncio.sleep", _hold)
    guild, player, track = await _playing()
    cog = OsaMusify(MagicMock())
    try:
        await _summon_player(cog, guild, player)
        await cog._panels[0].dismiss()
        assert cog._is_closed(guild) is False
        assert cog._panels == []
        await _summon_player(cog, guild, player)
        card = cog._panels[0]
        outside = _ask()
        outside.guild = guild
        outside.user.voice = None
        await card.close.callback(outside)
        assert "Join the voice channel" in outside.send.await_args.args[0]
        assert card._closed is False
        assert cog._is_closed(guild) is False

        player.stop.reset_mock()
        closer = _press(player)
        closer.guild = guild
        closer.response.is_done.return_value = False
        closer.delete_original_response = AsyncMock()
        await card.close.callback(closer)
        closer.delete_original_response.assert_awaited()
        player.stop.assert_not_awaited()
        assert card._closed is True
        assert cog._panels == []
        assert cog._is_closed(guild) is True
        assert _stored(guild).current.track.title == track.title

        await _song_ends(guild, player)
        started = await _play_link(monkeypatch, cog, guild, player.channel, "Ocean Drive")
        assert _stored(guild).current.track.title == "Ocean Drive"
        assert "Playing Ocean Drive" in started.edit_original_response.await_args.kwargs["content"]
        started.channel.send.assert_not_awaited()
        assert cog._panels == []

        again = _press(player)
        again.guild = guild
        again.response.is_done.return_value = False
        again.response.send_message = AsyncMock()
        posted = MagicMock()
        posted.delete = AsyncMock()
        posted.edit = AsyncMock()
        again.original_response = AsyncMock(return_value=posted)
        await cog.player.callback(cog, again)
        assert isinstance(again.response.send_message.await_args.kwargs["view"], MusicPlayer)
        assert len(cog._panels) == 1
        leaving = cog._panels[0]
        power = _press(player)
        power.guild = guild
        power.response.is_done.return_value = False
        power.delete_original_response = AsyncMock()
        await leaving.leave.callback(power)
        assert cog._is_closed(guild) is False
        assert leaving._closed is True
    finally:
        await _finish(cog)
        if guild.voice_client is not None:
            await stop_track(guild)

async def case_the_player_command_stays_down_when_the_song_is_over():
    """The song finished and nothing is lined up. The player command says so and posts no card."""
    guild, player, _track = await _playing()
    cog = OsaMusify(MagicMock())
    try:
        await _song_ends(guild, player)
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


async def test_one_playlist_card(monkeypatch, tmp_path):
    """One public playlist card. A failed send keeps it, the next one removes a card that could not close, and a second command waits. The card shows the line."""
    await case_one_public_playlist_card_per_server()
    await case_a_failed_playlist_send_keeps_the_open_one()
    await case_the_next_playlist_removes_a_card_that_could_not_close()
    await case_a_second_playlist_command_is_acknowledged_while_the_first_finishes()
    spot = tmp_path / "line"
    spot.mkdir()
    with monkeypatch.context() as patch:
        await case_playlist_shows_the_line(patch, spot)

async def case_one_public_playlist_card_per_server():
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
    opened = first.edit_original_response.await_args.kwargs["view"]
    assert first.response.defer.await_args.kwargs.get("ephemeral", False) is False

    other = _ask()
    other.guild.id = 31
    other.user.id = 8
    other.response.send_message = AsyncMock()
    other.response.is_done.return_value = False
    other.original_response = AsyncMock(return_value=other_message)
    await cog.playlist.callback(cog, other)
    first_message.delete.assert_awaited_once()
    assert opened.is_finished() is True
    theirs = other.edit_original_response.await_args.kwargs["view"]
    assert cog._playlist_cards[31] is theirs

    elsewhere = _ask()
    elsewhere.guild.id = 44
    elsewhere.user.id = 7
    elsewhere.response.send_message = AsyncMock()
    elsewhere.response.is_done.return_value = False
    elsewhere.original_response = AsyncMock(return_value=elsewhere_message)
    await cog.playlist.callback(cog, elsewhere)
    other_message.delete.assert_not_awaited()
    assert theirs.is_finished() is False
    other_server = elsewhere.edit_original_response.await_args.kwargs["view"]
    assert cog._playlist_cards[44] is other_server
    assert cog._playlist_cards[31] is theirs

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
    again = second.edit_original_response.await_args.kwargs["view"]
    assert second.response.defer.await_args.kwargs.get("ephemeral", False) is False
    assert cog._playlist_cards[31] is again
    assert cog._playlist_cards[44] is other_server
    again.stop()
    other_server.stop()


async def case_a_failed_playlist_send_keeps_the_open_one():
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
    opened = first.edit_original_response.await_args.kwargs["view"]

    failed = _ask()
    failed.guild.id = 31
    failed.edit_original_response = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(status=500), "no"),
    )
    failed.response.is_done.return_value = True
    await cog.playlist.callback(cog, failed)
    first_message.delete.assert_not_awaited()
    assert opened.is_finished() is False
    assert cog._playlist_cards[31] is opened
    opened.stop()

async def case_the_next_playlist_removes_a_card_that_could_not_close():
    cog = OsaMusify(MagicMock())
    first_message = MagicMock()
    first_message.id = 1
    first_message.delete = AsyncMock()
    second_message = MagicMock()
    second_message.id = 2
    second_message.delete = AsyncMock()
    first = _ask()
    first.guild.id = 31
    first.response.send_message = AsyncMock()
    first.response.is_done.return_value = False
    first.original_response = AsyncMock(return_value=first_message)
    await cog.playlist.callback(cog, first)
    opened = first.edit_original_response.await_args.kwargs["view"]
    opened.stop()
    assert opened.is_finished() is True
    first_message.delete.assert_not_awaited()

    second = _ask()
    second.guild.id = 31
    second.response.send_message = AsyncMock()
    second.response.is_done.return_value = False
    second.original_response = AsyncMock(return_value=second_message)
    await cog.playlist.callback(cog, second)
    first_message.delete.assert_awaited_once()
    again = second.edit_original_response.await_args.kwargs["view"]
    assert again.is_finished() is False
    assert cog._playlist_cards[31] is again
    again.stop()

async def case_a_second_playlist_command_is_acknowledged_while_the_first_finishes():
    cog = OsaMusify(MagicMock())
    first = _ask()
    first.guild.id = 31
    gate = asyncio.Event()
    started = asyncio.Event()

    async def hold_edit(**_kwargs):
        started.set()
        await gate.wait()

    first.edit_original_response = AsyncMock(side_effect=hold_edit)
    first_message = MagicMock()
    first_message.id = 1
    first_message.delete = AsyncMock()
    first.original_response = AsyncMock(return_value=first_message)
    opening = asyncio.create_task(cog.playlist.callback(cog, first))
    await asyncio.wait_for(started.wait(), timeout=2)

    second = _ask()
    second.guild.id = 31
    second_message = MagicMock()
    second_message.id = 2
    second_message.delete = AsyncMock()
    second.original_response = AsyncMock(return_value=second_message)
    pending = asyncio.create_task(cog.playlist.callback(cog, second))
    for _ in range(20):
        if second.response.defer.await_count:
            break
        await asyncio.sleep(0)
    assert second.response.defer.await_count == 1
    assert pending.done() is False
    gate.set()
    await opening
    await pending
    shown = second.edit_original_response.await_args.kwargs["view"]
    first_message.delete.assert_awaited()
    shown.stop()

async def case_playlist_shows_the_line(monkeypatch, tmp_path):
    assert OsaMusify(MagicMock()).guild_config is guild_config
    store = GuildConfig(tmp_path / "guild.json")
    cog = OsaMusify(MagicMock(), guild_config=store)
    monkeypatch.setattr(MusicPlayer, "follow", lambda self, message: None)

    interaction = _ask()
    interaction.guild.voice_client = None
    await cog.playlist.callback(cog, interaction)
    shown = interaction.edit_original_response.await_args.kwargs["view"]
    assert isinstance(shown, PlaylistCard)
    assert "Nothing is playing" in _text(shown)
    assert "Use /music play to play a song" in _text(shown)
    shown.stop()

    guild, _member, _player, _tracks, note = await _line_up(9301, ["Alpha", "Beta"])
    shown = None
    try:
        assert note == "Playing Alpha."
        interaction = _ask()
        interaction.guild = guild
        await cog.playlist.callback(cog, interaction)
        shown = interaction.edit_original_response.await_args.kwargs["view"]
        assert isinstance(shown, PlaylistCard)
        assert "### Now Playing" in _text(shown)
        assert "Alpha" in _text(shown)
        assert "Beta" in _text(shown)
    finally:
        if shown is not None:
            shown.stop()
        await stop_track(guild)
