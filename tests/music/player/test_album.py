"""The album card. No Discord login and no Lavalink node."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord

from music.panel import (
    LABEL_ADD_ALL,
    LABEL_LIKED,
    LABEL_PLAYER,
    LABEL_QUEUE,
    MARK_ADD,
    MARK_BACK,
    MARK_CLOSE,
    MARK_NEXT,
    page_line,
)
from music.player.album import ALBUM_SECONDS, ALBUM_TITLE, ALL_HINT, _AlbumCard, _album_line
from music.player.card import INFO_MARK, MusicPlayer
from music.playback import queued, stop_track
from tests.music.player.support import _fitted
from tests.music.support import _ask, _card_text, _named, _playing, _press, _scene


def case_an_album_line():
    """A long album name and artist clip. A short one names the record."""
    album = _album_line("雨" * 40, "雨" * 40, None)
    assert album.split("\n", 1)[0].endswith("…")
    assert album.split("\n", 1)[1].startswith("By ")
    assert album.split("\n", 1)[1].endswith("…")
    assert _album_line("Dreaming Out Loud", "OneRepublic", None) == (
        "### Dreaming Out Loud\nBy OneRepublic"
    )


def _album_rows(view):
    return [
        item
        for item in view.walk_children()
        if isinstance(item, discord.ui.Section) and getattr(item.accessory, "label", None) == MARK_ADD
    ]

def _page_text(view) -> str:
    for item in view.walk_children():
        if isinstance(item, discord.ui.TextDisplay) and item.content.startswith("Page "):
            return item.content
    return ""

async def test_a_paused_song_does_not_pull_the_album_into_voice(monkeypatch):
    """The bot already left after a pause. Adding from the album does not join."""
    from types import SimpleNamespace

    warmed = MagicMock()
    monkeypatch.setattr("music.player.album.paused_away", lambda guild: True)
    monkeypatch.setattr("music.player.album.warm_voice", warmed)
    monkeypatch.setattr("music.player.album.now_playing", lambda guild: SimpleNamespace(has_track=True))
    monkeypatch.setattr(
        "music.player.album.queue_tracks",
        AsyncMock(return_value="Added After. I Ain't Worried is still paused."),
    )
    card = _AlbumCard("Dreaming Out Loud", None, "OneRepublic", None, [_named("After")], None, "spotify")
    press = _ask()
    press.guild.voice_client = None
    await card.on_add(press, 0)
    warmed.assert_not_called()
    assert "still paused" in press.send.await_args.args[0]

    warmed.reset_mock()
    monkeypatch.setattr("music.player.album.paused_away", lambda guild: False)
    monkeypatch.setattr("music.player.album.now_playing", lambda guild: None)
    monkeypatch.setattr("music.player.album.queue_tracks", AsyncMock(return_value="Playing After."))
    monkeypatch.setattr("music.player.album.show_note", AsyncMock())
    monkeypatch.setattr("music.player.album.finish_note", AsyncMock())
    fresh = _ask()
    fresh.guild.voice_client = None
    await card.on_add(fresh, 0)
    warmed.assert_called_once()

async def test_an_album_add(monkeypatch):
    """Adding while a song is on stays quiet and leaves the album up. An idle album deletes itself."""
    with monkeypatch.context() as patch:
        await case_an_album_add_stays_quiet_while_a_song_is_playing(patch)
    with monkeypatch.context() as patch:
        await case_adding_while_a_song_is_on_leaves_the_album_up(patch)
    await case_an_idle_album_deletes_itself()

async def case_an_album_add_stays_quiet_while_a_song_is_playing(monkeypatch):
    from types import SimpleNamespace

    notes = []

    async def show(interaction, line, quote=False):
        notes.append(line)

    monkeypatch.setattr("music.player.album.show_note", show)
    monkeypatch.setattr("music.player.album.now_playing", lambda guild: SimpleNamespace(has_track=True))
    monkeypatch.setattr("music.player.album.queue_tracks", AsyncMock(return_value="Added After."))
    card = _AlbumCard("Dreaming Out Loud", None, "OneRepublic", None, [_named("After")], None, "spotify")
    press = _ask()
    await card.on_add(press, 0)
    assert notes == []
    press.response.defer.assert_awaited()
    press.send.assert_not_awaited()

    notes.clear()
    monkeypatch.setattr("music.player.album.now_playing", lambda guild: None)
    monkeypatch.setattr("music.player.album.queue_tracks", AsyncMock(return_value="Playing After."))
    press = _ask()
    await card.on_add(press, 0)
    assert notes == ["Starting After"]

class _AlbumClick(discord.Interaction):
    """A real component interaction. A plain mock never takes the delete path."""

    def __init__(self) -> None:
        response = MagicMock()
        response.is_done.return_value = False

        async def defer(*args, **kwargs):
            response.is_done.return_value = True

        response.defer = AsyncMock(side_effect=defer)
        self._state = None
        self._cs_response = response
        followup = MagicMock()
        followup.send = AsyncMock()
        self._cs_followup = followup
        self.delete_original_response = AsyncMock()
        self.edit_original_response = AsyncMock()
        self.send = AsyncMock()
        guild = MagicMock()
        guild.voice_client = MagicMock()
        self.user = MagicMock()
        self.user.guild = guild

async def case_adding_while_a_song_is_on_leaves_the_album_up(monkeypatch):
    """A button ack's original message is the album. Deleting it closes the card."""
    from types import SimpleNamespace

    monkeypatch.setattr("music.player.album.now_playing", lambda guild: SimpleNamespace(has_track=True))
    monkeypatch.setattr("music.player.album.queue_tracks", AsyncMock(return_value="Added After."))
    card = _AlbumCard("Dreaming Out Loud", None, "OneRepublic", None, [_named("After")], None, "spotify")
    press = _AlbumClick()
    await card.on_add(press, 0)
    press.delete_original_response.assert_not_awaited()
    press.edit_original_response.assert_not_awaited()
    press.followup.send.assert_not_awaited()
    assert card._closed is False

    monkeypatch.setattr("music.player.album.queue_tracks", AsyncMock(return_value="The queue is full."))
    full = _AlbumClick()
    await card.on_add(full, 0)
    full.delete_original_response.assert_not_awaited()
    full.edit_original_response.assert_not_awaited()
    full.followup.send.assert_awaited()
    sent = full.followup.send.await_args.kwargs
    assert sent["ephemeral"] is True
    assert "queue is full" in sent["content"]

async def case_an_idle_album_deletes_itself():
    card = _AlbumCard("Dreaming Out Loud", None, "OneRepublic", None, [], None, "spotify")
    assert card.timeout == ALBUM_SECONDS == 45
    message = MagicMock()
    message.delete = AsyncMock()
    card.message = message
    released = []
    card._release = released.append
    await card.on_timeout()
    message.delete.assert_awaited_once()
    assert released == [card]
    assert card._closed is True

async def case_the_album_button_stays_on_the_song_that_is_playing():
    """A mirror can lose its album. The previous song's album is not a substitute."""
    from types import SimpleNamespace

    guild, player, track = await _playing()
    try:
        view = MusicPlayer(guild)
        track.album = SimpleNamespace(name="Dreaming Out Loud", url=None)
        mirror = MagicMock()
        mirror.title = track.title
        mirror.author = track.author
        mirror.uri = "https://www.youtube.com/watch?v=mirror"
        mirror.album = None
        mirror.raw_data = {}
        mirror.encoded = ""
        player._track = mirror
        press = _press(player)
        press.response.send_message = AsyncMock()
        await view.info.callback(press)
        card = press.response.send_message.await_args.kwargs["view"]
        assert "Dreaming Out Loud" in _card_text(card)

        other = MagicMock()
        other.title = "Stop and Stare"
        other.author = "OneRepublic"
        other.uri = "https://example.com/other"
        other.album = None
        other.raw_data = {}
        other.encoded = ""
        player._track = other
        missed = _press(player)
        missed.send = AsyncMock()
        await view.info.callback(missed)
        assert "no album" in missed.send.await_args.args[0]
        missed.response.send_message.assert_not_called()
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)

async def case_a_different_video_does_not_take_the_album():
    """The same title on one site is still another recording."""
    from types import SimpleNamespace

    guild, player, track = await _playing()
    try:
        view = MusicPlayer(guild)
        track.uri = "https://www.youtube.com/watch?v=song"
        track.album = SimpleNamespace(name="Dreaming Out Loud", url=None)
        other = MagicMock()
        other.title = track.title
        other.author = track.author
        other.uri = "https://www.youtube.com/watch?v=other"
        other.album = None
        other.raw_data = {}
        other.encoded = ""
        player._track = other
        missed = _press(player)
        missed.send = AsyncMock()
        await view.info.callback(missed)
        assert "no album" in missed.send.await_args.args[0]
        missed.response.send_message.assert_not_called()
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)

async def test_the_info_button_shows_the_album(monkeypatch, tmp_path):
    """The line names the record. The button stays on this recording, opens it, Add All queues it, Liked saves it, art is not the page, a song can be loaded again, and two people each keep an album."""
    case_an_album_line()
    await _scene(case_the_album_button_stays_on_the_song_that_is_playing)
    await _scene(case_a_different_video_does_not_take_the_album)
    await _scene(case_the_info_button_opens_the_album, monkeypatch=monkeypatch)
    await _scene(case_add_all_queues_the_album, monkeypatch=monkeypatch)
    liked = tmp_path / "liked"
    liked.mkdir()
    await _scene(case_the_liked_list_saves_the_album_song, liked, monkeypatch=monkeypatch)
    await _scene(case_a_long_album_turns_the_page, monkeypatch=monkeypatch)
    await _scene(case_an_apple_link_opens_the_album, monkeypatch=monkeypatch)
    await _scene(case_a_second_open_replaces_the_album, monkeypatch=monkeypatch)
    case_a_spotify_album_id_stops_at_twenty_two_characters()
    await _scene(case_spotify_album_art_is_not_the_album_page, monkeypatch=monkeypatch)
    await _scene(case_a_spotify_song_is_loaded_again_to_open_its_album, monkeypatch=monkeypatch)
    await _scene(case_two_people_can_each_have_an_album_open, monkeypatch=monkeypatch)

def case_a_spotify_album_id_stops_at_twenty_two_characters():
    import base64

    from music.player.album import _encoded_album

    album_id = "37PoN4qrak5WenNfs1NcMr"
    page = f"https://open.spotify.com/album/{album_id}"
    track = MagicMock()
    track.encoded = base64.b64encode(page.encode() + b"extraALNUM99").decode()
    assert _encoded_album(track) == page

async def case_spotify_album_art_is_not_the_album_page(monkeypatch):
    import base64
    from types import SimpleNamespace

    from music.lavalink import Found

    guild, player, track = await _playing()
    try:
        view = MusicPlayer(guild)
        album_id = "37PoN4qrak5WenNfs1NcMr"
        page = f"https://open.spotify.com/album/{album_id}"
        track.album = SimpleNamespace(name=None, url="https://i.scdn.co/image/ab67616d")
        track.raw_data = {
            "pluginInfo": {
                "albumName": "Dreaming Out Loud",
                "albumArtUrl": "https://i.scdn.co/image/ab67616d",
                "save_uri": "https://open.spotify.com/track/1OQSLNZtIHsIseGlmwJBxG",
            },
        }
        track.uri = "https://open.spotify.com/track/1OQSLNZtIHsIseGlmwJBxG"
        # Lavalink stores the album address in the track bytes, not in pluginInfo.
        raw = b"\x00" + bytes([len(page)]) + page.encode()
        track.encoded = base64.b64encode(raw).decode()
        other = MagicMock()
        other.title = "Stop and Stare"
        other.author = "OneRepublic"
        other.length = 201_000
        other.uri = "https://open.spotify.com/track/other"
        other.artwork = None

        async def search(guild_id, song, platform="youtube", mode=None):
            assert song == page
            return Found(song, "spotify", [track, other], True, "Dreaming Out Loud")

        monkeypatch.setattr("music.player.album.find_songs", search)
        press = _press(player)
        press.followup.send = AsyncMock()
        await view.info.callback(press)
        text = _card_text(press.followup.send.await_args.kwargs["view"])
        assert "Dreaming Out Loud" in text
        assert "Stop and Stare" in text
        assert page in text
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)

def _album_track(title, uri):
    row = MagicMock()
    row.title = title
    row.author = "OneRepublic"
    row.length = 201_000
    row.uri = uri
    row.artwork = None
    return row


async def case_the_info_button_opens_the_album(monkeypatch):
    from types import SimpleNamespace

    from music.lavalink import Found

    guild, player, track = await _playing()
    try:
        view = MusicPlayer(guild)
        assert view.info.label == _fitted(INFO_MARK)
        outsider = MagicMock()
        outsider.send = AsyncMock()
        outsider.user.voice = None
        await view.info.callback(outsider)
        assert "Join the voice channel first" in outsider.send.await_args.args[0]

        missing = _press(player)
        missing.send = AsyncMock()
        await view.info.callback(missing)
        assert "no album" in missing.send.await_args.args[0]

        track.album = SimpleNamespace(name="Dreaming Out Loud", url=None)
        named = _press(player)
        named.response.send_message = AsyncMock()
        await view.info.callback(named)
        card = named.response.send_message.await_args.kwargs["view"]
        assert named.response.send_message.await_args.kwargs["ephemeral"] is True
        text = _card_text(card)
        assert ALBUM_TITLE in text
        assert "### Dreaming Out Loud" in text
        assert "By OneRepublic" in text
        assert "I Ain't Worried" in text
        assert "Playing" in text
        assert "OneRepublic" in text
        assert _album_rows(card)[0].accessory.label == MARK_ADD

        other = _album_track("Stop and Stare", "https://example.com/other")
        page = "https://open.spotify.com/album/abc"
        track.album = SimpleNamespace(name="Dreaming Out Loud", url=page)

        async def search(_guild_id, song, platform="youtube"):
            assert song == page
            return Found(song, "spotify", [track, other], True, "Dreaming Out Loud")

        monkeypatch.setattr("music.player.album.find_songs", search)
        press = _press(player)
        press.followup.send = AsyncMock()
        await view.info.callback(press)
        press.response.defer.assert_awaited()
        opened = press.followup.send.await_args.kwargs
        assert opened["ephemeral"] is True
        shown = _card_text(opened["view"])
        assert ALBUM_TITLE in shown
        assert "### [Dreaming Out Loud](https://open.spotify.com/album/abc)" in shown
        assert "By OneRepublic" in shown
        assert "Stop and Stare" in shown
        assert shown.count("Playing") == 1
        rows = _album_rows(opened["view"])
        assert [row.accessory.label for row in rows] == [MARK_ADD, MARK_ADD]
        assert _page_text(opened["view"]).startswith(page_line(1, 1, 2, "Track"))
        footer = [
            item for item in opened["view"].walk_children() if isinstance(item, discord.ui.ActionRow)
        ][-1]
        assert LABEL_ADD_ALL.split("Add All") == LABEL_PLAYER.split("Player")
        assert LABEL_LIKED.split("Liked") == LABEL_QUEUE.split("Queue")
        assert [button.label for button in footer.children] == [
            MARK_BACK, MARK_NEXT, LABEL_ADD_ALL, LABEL_LIKED, MARK_CLOSE,
        ]
        assert len(ALL_HINT) <= 100
        thumbs = [
            item for item in opened["view"].walk_children() if isinstance(item, discord.ui.Thumbnail)
        ]
        assert [thumb.media.url for thumb in thumbs] == ["https://example.com/art.jpg"]
        heading = opened["view"].children[0].children[0]
        assert isinstance(heading, discord.ui.Section)
        assert heading.accessory is thumbs[0]
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)


async def case_add_all_queues_the_album(monkeypatch):
    from types import SimpleNamespace

    from music.lavalink import Found

    guild, player, track = await _playing()
    try:
        other = _album_track("Stop and Stare", "https://example.com/other")
        page = "https://open.spotify.com/album/abc"
        track.album = SimpleNamespace(name="Dreaming Out Loud", url=page)

        async def search(_guild_id, song, platform="youtube"):
            assert song == page
            return Found(song, "spotify", [track, other], True, "Dreaming Out Loud")

        monkeypatch.setattr("music.player.album.find_songs", search)
        view = MusicPlayer(guild)
        press = _press(player)
        press.followup.send = AsyncMock()
        await view.info.callback(press)
        album = press.followup.send.await_args.kwargs["view"]
        ask = _press(player)
        ask.guild = guild
        ask.response.send_modal = AsyncMock()
        await album._all.callback(ask)
        modal = ask.response.send_modal.await_args.args[0]
        assert modal.title == "Add All"
        assert modal.query.text == "This adds every song on the album."
        assert modal.query.description == ALL_HINT
        assert 'Type "yes" to confirm' in modal.query.description
        assert modal.query.component.placeholder == "yes"
        modal.query.component._value = "no"
        declined = _press(player)
        declined.guild = guild
        declined.send = AsyncMock()
        await modal.on_submit(declined)
        declined.send.assert_not_awaited()
        assert [row.title for row in queued(guild) if not row.playing] == []
        modal.query.component._value = "Yes"
        accepted = _press(player)
        accepted.guild = guild
        accepted.send = AsyncMock()
        await modal.on_submit(accepted)
        assert [row.title for row in queued(guild) if not row.playing] == [
            "I Ain't Worried",
            "Stop and Stare",
        ]
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)


async def case_the_liked_list_saves_the_album_song(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from guild import GuildConfig
    from music.archive import create_playlist, find_playlist
    from music.lavalink import Found

    guild, player, track = await _playing()
    try:
        other = _album_track("Stop and Stare", "https://example.com/other")
        page = "https://open.spotify.com/album/abc"
        track.album = SimpleNamespace(name="Dreaming Out Loud", url=page)

        async def search(_guild_id, song, platform="youtube"):
            assert song == page
            return Found(song, "spotify", [track, other], True, "Dreaming Out Loud")

        monkeypatch.setattr("music.player.album.find_songs", search)
        store = GuildConfig(tmp_path / "album.json")
        create_playlist(store, guild.id, "Evening", 17)
        view = MusicPlayer(guild)
        view._lists = store
        press = _press(player)
        press.followup.send = AsyncMock()
        await view.info.callback(press)
        album = press.followup.send.await_args.kwargs["view"]
        assert album._queue.label == LABEL_LIKED
        assert album._queue.style == discord.ButtonStyle.secondary
        switch = _press(player)
        await album._queue.callback(switch)
        assert album._queue.label == LABEL_LIKED
        assert album._queue.style == discord.ButtonStyle.primary
        assert [album._adds[0].label, album._adds[1].label] == [MARK_ADD, MARK_ADD]
        assert album._adds[0].method == "on_keep"
        keep = _press(player)
        keep.user.id = 17
        keep.response.send_modal = AsyncMock()
        await album._adds[0].callback(keep)
        modal = keep.response.send_modal.await_args.args[0]
        assert modal.title == "Save"
        modal.menu._values = ["Evening"]
        submit = _press(player)
        submit.user.id = 17
        submit.send = AsyncMock()
        await modal.on_submit(submit)
        assert "Saved to Evening" in submit.send.await_args.args[0]
        saved = find_playlist(store, guild.id, "Evening")
        assert saved.songs[0].title == track.title
        back = _press(player)
        await album._queue.callback(back)
        assert album._queue.label == LABEL_LIKED
        assert album._queue.style == discord.ButtonStyle.secondary
        assert [album._adds[0].label, album._adds[1].label] == [MARK_ADD, MARK_ADD]
        assert album._adds[0].method == "on_add"
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)


async def case_a_long_album_turns_the_page(monkeypatch):
    from types import SimpleNamespace

    from music.lavalink import Found

    guild, player, track = await _playing()
    try:
        track.album = SimpleNamespace(name="Dreaming Out Loud", url="https://open.spotify.com/album/abc")
        long = [
            _album_track(f"Song {number}", f"https://example.com/{number}")
            for number in range(200)
        ]
        for row in long:
            row.length = 180_000

        async def many(_guild_id, _song, platform="youtube"):
            return Found(_song, "spotify", long, True, "Dreaming Out Loud")

        monkeypatch.setattr("music.player.album.find_songs", many)
        view = MusicPlayer(guild)
        again = _press(player)
        again.followup.send = AsyncMock()
        await view.info.callback(again)
        packed_view = again.followup.send.await_args.kwargs["view"]
        packed = _card_text(packed_view)
        assert "Song 0" in packed
        assert "Song 4" in packed
        assert "Song 5" not in packed
        assert "Song 199" not in packed
        assert _page_text(packed_view).startswith(page_line(1, 40, 200, "Track"))
        turn = _press(player)
        await packed_view._next.callback(turn)
        turned = _card_text(packed_view)
        assert "Song 5" in turned
        assert "Song 0" not in turned
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)


async def case_an_apple_link_opens_the_album(monkeypatch):
    from types import SimpleNamespace

    from music.lavalink import Found

    guild, player, track = await _playing()
    try:
        track.album = SimpleNamespace(name=None, url=None)
        track.raw_data = {}
        track.uri = "https://music.apple.com/us/album/chet-baker-sings/1440812340?i=1440811111"

        async def apple(_guild_id, song, platform="youtube"):
            assert song == "https://music.apple.com/us/album/chet-baker-sings/1440812340"
            return Found(song, "apple", [track], True, "Chet Baker Sings")

        monkeypatch.setattr("music.player.album.find_songs", apple)
        view = MusicPlayer(guild)
        apple_press = _press(player)
        apple_press.followup.send = AsyncMock()
        await view.info.callback(apple_press)
        apple_card = _card_text(apple_press.followup.send.await_args.kwargs["view"])
        assert "Chet Baker Sings" in apple_card
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)


async def case_a_second_open_replaces_the_album(monkeypatch):
    from types import SimpleNamespace

    from music.lavalink import Found

    guild, player, track = await _playing()
    try:
        page = "https://open.spotify.com/album/abc"
        track.album = SimpleNamespace(name="Dreaming Out Loud", url=page)

        async def search(_guild_id, song, platform="youtube"):
            assert song == page
            return Found(song, "spotify", [track], True, "Dreaming Out Loud")

        monkeypatch.setattr("music.player.album.find_songs", search)
        view = MusicPlayer(guild)
        first = AsyncMock()
        first.delete = AsyncMock()
        one = _press(player)
        one.user.id = 17
        one.followup.send = AsyncMock(return_value=first)
        await view.info.callback(one)
        first.delete.assert_not_awaited()

        second = AsyncMock()
        second.delete = AsyncMock()
        two = _press(player)
        two.user.id = 17
        two.followup.send = AsyncMock(return_value=second)
        await view.info.callback(two)
        first.delete.assert_awaited_once()
        assert one.followup.send.await_args.kwargs["view"].is_finished()
        second.delete.assert_not_awaited()
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)

async def case_a_spotify_song_is_loaded_again_to_open_its_album(monkeypatch):
    from types import SimpleNamespace

    from music.lavalink import Found

    guild, player, track = await _playing()
    try:
        view = MusicPlayer(guild)
        page = "https://open.spotify.com/album/0ETFjACtuP2ADo6LFhL6HN"
        track.album = SimpleNamespace(name="Abbey Road", url=None)
        track.raw_data = {"pluginInfo": {"save_uri": "https://open.spotify.com/track/2EqlS6tkEnglzr7tkKAAYD"}}
        track.uri = "https://open.spotify.com/track/2EqlS6tkEnglzr7tkKAAYD"
        track.encoded = ""
        fresh = MagicMock()
        fresh.album = SimpleNamespace(name="Abbey Road", url=page)
        fresh.uri = track.uri
        fresh.author = "The Beatles"
        fresh.title = track.title
        fresh.artwork = None
        fresh.encoded = ""
        fresh.raw_data = {}
        fresh.length = track.length
        other = MagicMock()
        other.title = "Something"
        other.author = "The Beatles"
        other.length = 182_000
        other.uri = "https://open.spotify.com/track/other"
        other.artwork = None
        calls = []

        async def search(guild_id, song, platform="youtube", mode=None):
            calls.append((song, mode))
            if "/track/" in song:
                return Found(song, "spotify", [fresh], True, None)
            return Found(song, "spotify", [fresh, other], True, "Abbey Road")

        monkeypatch.setattr("music.player.album.find_songs", search)
        press = _press(player)

        async def defer(*args, **kwargs):
            press.response.is_done.return_value = True

        press.response.defer.side_effect = defer
        press.followup.send = AsyncMock()
        await view.info.callback(press)
        assert calls[0] == ("https://open.spotify.com/track/2EqlS6tkEnglzr7tkKAAYD", "track")
        assert calls[1] == (page, None)
        text = _card_text(press.followup.send.await_args.kwargs["view"])
        assert "Something" in text
        assert page in text
    finally:
        if guild.voice_client is not None:
            await stop_track(guild)

async def case_two_people_can_each_have_an_album_open(monkeypatch):
    """Each person has their own album. A second press only replaces that person's."""
    from types import SimpleNamespace

    from music.lavalink import Found

    guild, player, track = await _playing()
    view = None
    try:
        page = "https://open.spotify.com/album/37PoN4qrak5WenNfs1NcMr"
        track.album = SimpleNamespace(name="Dreaming Out Loud", url=page)

        async def search(*_args, **_kwargs):
            return Found(page, "spotify", [track], True, name="Dreaming Out Loud")

        monkeypatch.setattr("music.player.album.find_songs", search)
        view = MusicPlayer(guild)
        sent = []

        async def send(**_kwargs):
            message = MagicMock()
            message.delete = AsyncMock()
            sent.append(message)
            return message

        first = _press(player)
        first.user.id = 18
        first.followup.send = AsyncMock(side_effect=send)
        first.response.defer = AsyncMock()
        other = _press(player)
        other.user.id = 19
        other.followup.send = AsyncMock(side_effect=send)
        other.response.defer = AsyncMock()
        await view.info.callback(first)
        await view.info.callback(other)
        await view.info.callback(first)
        sent[0].delete.assert_awaited_once()
        sent[1].delete.assert_not_awaited()
        assert view._albums[18][0] is sent[2]
        assert view._albums[19][0] is sent[1]
        assert view._albums[19][1]._closed is False
    finally:
        if view is not None:
            view.shutdown()
        if guild.voice_client is not None:
            await stop_track(guild)
