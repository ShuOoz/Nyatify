"""Clocks, song lines, and idle cards. No Discord login and no Lavalink node."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from music.panel import (
    PANEL_SECONDS,
    _CARD,
    _columns,
    _hold_card,
    format_clock,
    page_line,
    progress_bar,
    progress_line,
    progress_text,
    song_label,
    song_line,
    song_row,
)
from music.play.discover import EMPTY_SECONDS, GenreList
from music.player.view import MusicPlayer
from music.playlist.card import LiveLine
from music.playback import stop_track

from tests.music.support import _playing

def test_clock_and_song_line():
    assert format_clock(0) == "0:00"
    assert format_clock(148_000) == "2:28"
    assert format_clock(3_600_000) == "1:00:00"
    assert format_clock(-20) == "0:00"
    assert progress_text(0, 148_000) == "0:00 / 2:28"
    assert progress_text(0, 0) == "live"
    assert progress_line(0, 148_000) == f"0:00  {progress_bar(0, 148_000)}  2:28"
    assert progress_line(0, 0).startswith("live  ")
    assert progress_bar(0, 0) == progress_bar(50_000, 0)
    assert "━" not in progress_bar(0, 0)
    assert progress_bar(0, 148_000).startswith("●")
    assert progress_bar(148_000, 148_000).endswith("●")
    assert progress_bar(0, 148_000) != progress_bar(74_000, 148_000)
    assert song_line("I Ain't Worried", "OneRepublic", "https://example.com/song") == (
        "**[I Ain't Worried](https://example.com/song)**\nOneRepublic"
    )
    assert song_line("I Ain't Worried - OneRepublic", "OneRepublic", None) == (
        "**I Ain't Worried - OneRepublic**"
    )
    assert song_line("Needed", "Ed", None) == "**Needed**\nEd"
    assert song_line("Needed, Ed", "Ed", None) == "**Needed, Ed**"
    assert song_line("Airplane", "Air", None) == "**Airplane**\nAir"
    assert song_label("Needed", "Ed") == "Needed - Ed"
    assert song_label("Needed, Ed", "Ed") == "Needed, Ed"
    assert song_label("Airplane", "Air") == "Airplane - Air"

def test_song_row_names_the_artist_and_the_length():
    assert song_row(1, "sunny days", "wave to earth", 248_000) == (
        "**1\\.** sunny days\n-# \u2007\u2008\u00a0wave to earth • 4:08"
    )
    assert song_row(4, "Back To Me", "The Rose", 224_000) == (
        "**4\\.** Back To Me\n-# \u2007\u2008\u00a0The Rose • 3:44"
    )
    assert song_row(10, "Later", "Band", 0) == "**10\\.** Later\n-# \u2007\u2007\u2008\u00a0Band"
    assert song_row(3, "  ", "", 0) == "**3\\.** Unknown"
    assert song_row(1, "First", "Mac", 182_000, "<@4>").endswith("Mac • 3:02 • <@4>")
    assert song_row(2, "x" * 90, "A", 0).split("\n", 1)[0].endswith("…")
    assert song_row(3, "cherry flavoured love inside your heart", "wave to earth", 268_000).split("\n", 1)[0] == (
        "**3\\.** cherry flavoured love insi…"
    )
    assert song_row(3, "Please Come Home (feat. CARRTOON)", "Mac Ayres", 225_000).split("\n", 1)[0] == (
        "**3\\.** Please Come Home (feat…"
    )
    assert song_row(100, "Please Come Home (feat. CARRTOON)", "Mac", 0).split("\n", 1)[0] == (
        "**100\\.** Please Come Home (fe…"
    )
    assert song_row(2, "雨" * 20, "POP", 0).split("\n", 1)[0] == f"**2\\.** {'雨' * 12}…"
    held = _hold_card("Page 1/1  |  1 Result")
    assert held.startswith("Page 1/1  |  1 Result")
    assert _columns(held) >= _CARD - 1
    wide = page_line(1, 2, 6)
    assert wide == "Page 1/2  |  6 Results"
    single = page_line(1, 1, 1)
    assert single == "Page 1/1  |  1 Result"

async def test_an_idle_music_card_is_deleted():
    message = MagicMock()
    message.delete = AsyncMock()

    genre = GenreList("spotify", AsyncMock())
    assert genre.timeout == EMPTY_SECONDS
    genre.message = message
    await genre.on_timeout()
    message.delete.assert_awaited_once()

    quiet = MagicMock()
    quiet.voice_client = None
    line = LiveLine(quiet, MagicMock())
    assert line.timeout == PANEL_SECONDS
    message.delete.reset_mock()
    line.message = message
    await line.on_timeout()
    message.delete.assert_awaited_once()

    playing, _player, _track = await _playing()
    try:
        card = MusicPlayer(playing)
        assert card.timeout is None
        card.message = message
        message.delete.reset_mock()
        await card.dismiss()
        message.delete.assert_awaited_once()
        assert card._closed is True
    finally:
        await stop_track(playing)

    kept = GenreList("spotify", AsyncMock())
    kept.message = message
    message.delete.reset_mock()
    kept.stop()
    message.delete.assert_not_awaited()
