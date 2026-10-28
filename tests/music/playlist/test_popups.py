"""The words that empty a list or remove it. No Discord login and no Lavalink node."""

from __future__ import annotations

from music.playlist.popups import empty_hint, means_remove, means_yes, remove_hint
from music.playlist.card import HISTORY, QUEUE

def test_yes_is_the_only_word_that_empties():
    assert means_yes("YES")
    assert means_yes("yes")
    assert means_yes("Yes")
    assert means_yes("  YeS  ")
    assert means_yes("no") is False
    assert means_yes("y") is False
    assert means_yes("ok") is False
    assert means_yes("") is False
    assert means_yes("yes please") is False
    assert empty_hint(QUEUE) == 'This permanently clears every waiting song. Type "yes" to confirm'
    assert empty_hint(HISTORY) == 'This permanently clears every played song. Type "yes" to confirm'
    assert len(empty_hint(QUEUE)) <= 100
    assert means_remove("yes, remove it")
    assert means_remove("Yes, remove it")
    assert means_remove("YES, REMOVE IT")
    assert means_remove("  Yes, Remove It  ")
    assert means_remove("no") is False
    assert means_remove("yes, remove it please") is False
    assert means_remove("yes remove it") is False
    assert means_remove("yes, delete it") is False
    assert remove_hint() == (
        'This permanently removes this playlist. Type "yes, remove it" to confirm'
    )
    assert len(remove_hint()) <= 100
