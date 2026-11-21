"""Create, empty, remove, and save ask before they change the list. No Discord login."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from music.archive import SavedList, SavedSong
from music.playlist.card import HISTORY, QUEUE
from music.playlist.popups import (
    EmptyList,
    FindInList,
    NamePlaylist,
    QueueList,
    RemovePlaylist,
    SaveSong,
    empty_hint,
    remove_hint,
)


def _press():
    press = MagicMock()
    press.response.is_done.return_value = True
    return press


async def test_a_popup_changes_the_list_only_after_the_phrase(monkeypatch):
    """A blank name, or any word but the phrase, leaves the list. The phrase runs it."""
    notes: list[str] = []
    deferred: list[object] = []

    async def note(_interaction, line):
        notes.append(line)

    async def defer(interaction):
        deferred.append(interaction)

    monkeypatch.setattr("music.playlist.popups._note", note)
    monkeypatch.setattr("music.playlist.popups._defer", defer)
    apply = AsyncMock()

    create = NamePlaylist(apply)
    create.query.component._value = "   "
    await create.on_submit(_press())
    assert notes == ["Name the playlist."]
    apply.assert_not_awaited()

    create.query.component._value = "  Evening   jazz "
    await create.on_submit(_press())
    apply.assert_awaited_once()
    assert apply.await_args.args[1] == "Evening jazz"

    apply.reset_mock()
    empty = EmptyList(apply, QUEUE)
    assert empty.query.description == empty_hint(QUEUE)
    empty.query.component._value = "yes please"
    refused = _press()
    await empty.on_submit(refused)
    assert deferred == [refused]
    apply.assert_not_awaited()
    empty.query.component._value = "YES"
    await empty.on_submit(_press())
    apply.assert_awaited_once()
    assert apply.await_args.args[1] == QUEUE

    yours = EmptyList(apply, HISTORY, yours=True)
    assert yours.query.description == empty_hint(HISTORY, yours=True)

    apply.reset_mock()
    deferred.clear()
    remove = RemovePlaylist(apply)
    assert remove.query.description == remove_hint()
    remove.query.component._value = "yes"
    refused = _press()
    await remove.on_submit(refused)
    assert deferred == [refused]
    apply.assert_not_awaited()
    remove.query.component._value = "Yes, remove it"
    await remove.on_submit(_press())
    apply.assert_awaited_once()

    apply.reset_mock()
    notes.clear()
    find = FindInList(apply)
    find.query.component._value = "  "
    await find.on_submit(_press())
    assert notes == ["Name a song or an artist."]
    apply.assert_not_awaited()
    find.query.component._value = "  ocean "
    await find.on_submit(_press())
    assert apply.await_args.args[1] == "ocean"

    saved = SavedList("Evening", 4, (SavedSong("One", "youtube", "https://example.com/one"),))
    apply.reset_mock()
    notes.clear()
    queue = QueueList([saved], apply)
    await queue.on_submit(_press())
    assert notes == ["Pick a playlist."]
    apply.assert_not_awaited()
    queue.menu._values = ["Evening"]
    await queue.on_submit(_press())
    assert apply.await_args.args[1] == "Evening"

    apply.reset_mock()
    save = SaveSong([saved], apply)
    save.menu._values = ["Evening"]
    await save.on_submit(_press())
    assert apply.await_args.args[1] == "Evening"
