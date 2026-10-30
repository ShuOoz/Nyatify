"""Last.fm names a similar song. No Discord login and no network."""

from __future__ import annotations

import json

from music.player.radio import radio_seed, read_similar, similar_songs


def test_radio_seed_keeps_a_real_artist_and_drops_upload_tags():
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


def test_read_similar_keeps_pairs_and_drops_an_error():
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


async def test_similar_songs_uses_the_key_and_survives_a_bad_body(monkeypatch):
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
