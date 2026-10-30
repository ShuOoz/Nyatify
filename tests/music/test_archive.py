"""Saved playlists on disk. No Discord login and no Lavalink node."""

from __future__ import annotations

from guild import GuildConfig
from music.archive import (
    PLAYLISTS as SAVED_LISTS,
    SavedList,
    SavedSong,
    SONG_LIMIT,
    create_playlist,
    delete_playlist,
    drop_song,
    find_playlist,
    list_playlists,
    playlist_name,
    replace_songs,
    save_song,
)

def test_a_saved_song_keeps_its_link(tmp_path):
    assert playlist_name("  evening   jazz ") == "evening jazz"
    assert playlist_name("   ") is None
    assert playlist_name("e" * 100) == "e" * 80
    long_store = GuildConfig(tmp_path / "long.json")
    assert create_playlist(long_store, 7, "n" * 80 + "a", 4) == (
        "A playlist name can be 80 characters."
    )
    assert create_playlist(long_store, 7, "n" * 80 + "b", 4) == (
        "A playlist name can be 80 characters."
    )
    assert list_playlists(long_store, 7) == []
    assert isinstance(create_playlist(long_store, 7, "n" * 80, 4), SavedList)

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
        ("Two", "spotify"),
        ("One Wish", "youtube"),
    ]
    duplicate = save_song(
        store, 7, "Evening", SavedSong("One Wish", "youtube", "https://example.com/one"), 4,
    )
    assert duplicate == "One Wish is already in the playlist."
    assert [item.title for item in find_playlist(store, 7, "Evening").songs] == [
        "Two",
        "One Wish",
    ]
    reloaded = GuildConfig(store.path)
    assert find_playlist(reloaded, 7, "EVENING").songs == again.songs
    rich = save_song(
        store,
        7,
        "Evening",
        SavedSong("Three", "apple", "https://example.com/three", "  Mac Ayres ", 182_000),
        4,
    )
    assert isinstance(rich, SavedList)
    assert rich.songs[0] == SavedSong(
        "Three", "apple", "https://example.com/three", "Mac Ayres", 182_000,
    )
    assert find_playlist(GuildConfig(store.path), 7, "Evening").songs[0] == rich.songs[0]

    store.set_guild_list(8, SAVED_LISTS, [
        "nope",
        {"name": " ", "songs": []},
        {
            "name": "Night",
            "saver_id": True,
            "songs": [
                {"title": "Drop", "platform": "youtube", "url": "ftp://x"},
                {
                    "title": "  One Wish  ",
                    "platform": "Spotify",
                    "url": "https://example.com/a",
                    "author": "  Mac ",
                    "length": 182000,
                },
                {
                    "title": "Plain",
                    "platform": "youtube",
                    "url": "https://example.com/b",
                    "author": 5,
                    "length": True,
                },
            ],
        },
    ])
    found = list_playlists(store, 8)
    assert len(found) == 1
    assert found[0].name == "Night"
    assert found[0].saver_id is None
    assert found[0].songs == (
        SavedSong("One Wish", "spotify", "https://example.com/a", "Mac", 182000),
        SavedSong("Plain", "youtube", "https://example.com/b"),
    )

def test_the_same_recording_is_saved_once(tmp_path):
    store = GuildConfig(tmp_path / "guild.json")
    first = save_song(
        store,
        7,
        "Evening",
        SavedSong("One", "youtube", "https://youtu.be/abc?si=share"),
        4,
    )
    assert isinstance(first, SavedList)
    assert first.songs[0].url == "https://www.youtube.com/watch?v=abc"
    again = save_song(
        store,
        7,
        "Evening",
        SavedSong("One", "youtube", "https://music.youtube.com/watch?v=abc&si=track&t=12"),
        4,
    )
    assert again == "One is already in the playlist."
    short = save_song(
        store,
        7,
        "Evening",
        SavedSong("One", "youtube", "https://www.youtube.com/shorts/abc"),
        4,
    )
    assert short == "One is already in the playlist."
    spotify = save_song(
        store,
        7,
        "Evening",
        SavedSong("Two", "spotify", "https://open.spotify.com/track/xyz?si=1"),
        4,
    )
    assert isinstance(spotify, SavedList)
    assert spotify.songs[0].url == "https://open.spotify.com/track/xyz"
    duplicate = save_song(
        store,
        7,
        "Evening",
        SavedSong("Two", "spotify", "https://open.spotify.com/intl-en/track/xyz"),
        4,
    )
    assert duplicate == "Two is already in the playlist."
    made = create_playlist(
        store,
        7,
        "Morning",
        4,
        (
            SavedSong("One", "youtube", "https://youtu.be/abc"),
            SavedSong("One again", "youtube", "https://www.youtube.com/watch?v=abc&feature=share"),
            SavedSong("Tail", "youtube", "https://example.com/tail"),
        ),
    )
    assert isinstance(made, SavedList)
    assert [song.title for song in made.songs] == ["One", "Tail"]
    assert [song.title for song in find_playlist(store, 7, "Evening").songs] == ["Two", "One"]
    share = "https://music.apple.com/us/album/animals/1533388849?i=1533388859"
    apple = save_song(
        store,
        7,
        "Evening",
        SavedSong("Animals", "apple", share + "&uo=4"),
        4,
    )
    assert isinstance(apple, SavedList)
    assert apple.songs[0].url == share
    assert save_song(
        store,
        7,
        "Evening",
        SavedSong("Animals", "apple", "https://music.apple.com/us/song/1533388859"),
        4,
    ) == "Animals is already in the playlist."

def test_a_saved_list_stays_inside_its_limits(tmp_path):
    """A server keeps five playlists of a hundred songs. A list can lose a song or itself."""
    spot = tmp_path / "cap"
    spot.mkdir()
    case_a_server_keeps_five_playlists_of_a_hundred_songs(spot)
    spot = tmp_path / "drop"
    spot.mkdir()
    case_a_list_can_lose_a_song_or_itself(spot)

def case_a_server_keeps_five_playlists_of_a_hundred_songs(tmp_path):
    store = GuildConfig(tmp_path / "guild.json")
    song = SavedSong("One", "youtube", "https://example.com/one")
    made = create_playlist(store, 7, "  Evening  ", 4)
    assert isinstance(made, SavedList)
    assert made.name == "Evening"
    assert made.saver_id == 4
    assert made.songs == ()
    assert find_playlist(GuildConfig(store.path), 7, "EVENING").songs == ()
    assert create_playlist(store, 7, "evening", 9) == "That name is already a playlist."
    assert create_playlist(store, 7, "   ", 4) == "Name the playlist."
    for index in range(4):
        created = create_playlist(store, 7, f"List {index}", 4)
        assert isinstance(created, SavedList)
    assert create_playlist(store, 7, "Sixth", 4) == "A server keeps 5 playlists."
    assert save_song(store, 7, "Extra", song, 4) == "A server keeps 5 playlists."
    added = save_song(store, 7, "Evening", song, 9)
    assert isinstance(added, SavedList)
    assert [item.title for item in added.songs] == ["One"]
    assert [item.name for item in list_playlists(store, 7)] == [
        "Evening", "List 0", "List 1", "List 2", "List 3",
    ]

    songs = [
        {"title": f"Song {index}", "platform": "youtube", "url": f"https://example.com/{index}"}
        for index in range(99)
    ]
    store.set_guild_list(7, SAVED_LISTS, [{"name": "Full", "saver_id": 4, "songs": songs}])
    room = save_song(store, 7, "Full", song, 4)
    assert isinstance(room, SavedList)
    assert len(room.songs) == 100
    assert save_song(store, 7, "Full", song, 4) == "One is already in the playlist."
    extra = SavedSong("Extra", "youtube", "https://example.com/extra")
    assert save_song(store, 7, "Full", extra, 4) == "A playlist keeps 100 songs."
    assert len(find_playlist(store, 7, "Full").songs) == 100

def case_a_list_can_lose_a_song_or_itself(tmp_path):
    store = GuildConfig(tmp_path / "guild.json")
    made = create_playlist(
        store,
        7,
        "Evening",
        4,
        (
            SavedSong("One", "youtube", "https://example.com/one"),
            SavedSong("Two", "spotify", "https://example.com/two"),
            SavedSong("Skip", "youtube", "notaurl"),
        ),
    )
    assert isinstance(made, SavedList)
    assert [item.title for item in made.songs] == ["One", "Two"]
    updated = drop_song(store, 7, "EVENING", 0)
    assert isinstance(updated, SavedList)
    assert [item.title for item in updated.songs] == ["Two"]
    assert drop_song(store, 7, "Evening", 3) == "That song is gone."
    gone = delete_playlist(store, 7, "evening")
    assert isinstance(gone, SavedList)
    assert gone.name == "Evening"
    assert list_playlists(store, 7) == []
    assert delete_playlist(store, 7, "Evening") == "That playlist is gone."
    many = tuple(
        SavedSong(str(index), "youtube", f"https://example.com/{index}")
        for index in range(SONG_LIMIT + 5)
    )
    long = create_playlist(store, 7, "Long", 4, many)
    assert isinstance(long, SavedList)
    assert len(long.songs) == SONG_LIMIT
    assert long.songs[-1].title == str(SONG_LIMIT - 1)

def test_playlist_names_and_copies(tmp_path):
    """Lookalike names are one list, and a loaded list keeps one copy up to the cap."""
    spot = tmp_path / "part0"
    spot.mkdir()
    case_lookalike_playlist_names_are_one_list(spot)
    spot = tmp_path / "part1"
    spot.mkdir()
    case_a_loaded_list_keeps_one_copy_up_to_the_cap(spot)

def case_lookalike_playlist_names_are_one_list(tmp_path):
    store = GuildConfig(tmp_path / "guild.json")
    assert playlist_name("Jazz\u200b") == "Jazz"
    assert playlist_name("Cafe\u0301") == playlist_name("Café")
    made = create_playlist(store, 7, "Café", 4)
    assert isinstance(made, SavedList)
    assert create_playlist(store, 7, "Cafe\u0301", 9) == "That name is already a playlist."
    assert create_playlist(store, 7, "Jazz\u200b", 4).name == "Jazz"
    store.set_guild_list(8, SAVED_LISTS, [
        {
            "name": "Café",
            "songs": [{"title": "A", "platform": "youtube", "url": "https://example.com/a"}],
        },
        {
            "name": "Cafe\u0301",
            "songs": [{"title": "B", "platform": "youtube", "url": "https://example.com/b"}],
        },
        {"name": "rock", "songs": []},
        {"name": "Rock", "songs": []},
    ])
    found = list_playlists(store, 8)
    assert [saved.name for saved in found] == ["Café", "rock"]
    assert [song.title for song in found[0].songs] == ["A", "B"]


def case_a_loaded_list_keeps_one_copy_up_to_the_cap(tmp_path):
    store = GuildConfig(tmp_path / "guild.json")
    songs = [
        {"title": str(index), "platform": "youtube", "url": f"https://example.com/{index}"}
        for index in range(SONG_LIMIT + 25)
    ]
    songs.append({"title": "Again", "platform": "youtube", "url": "https://example.com/0"})
    store.set_guild_list(7, SAVED_LISTS, [{"name": "Big", "songs": songs}])
    loaded = find_playlist(store, 7, "Big")
    assert loaded is not None
    assert len(loaded.songs) == SONG_LIMIT
    assert loaded.songs[0].url == "https://example.com/0"
    assert all(song.title != "Again" for song in loaded.songs)
    assert save_song(
        store, 7, "Big", SavedSong("New", "youtube", "https://example.com/new"), 4,
    ) == "A playlist keeps 100 songs."

    replaced = replace_songs(
        store,
        7,
        "Big",
        tuple(SavedSong(str(index % 3), "youtube", f"https://example.com/{index % 3}") for index in range(10)),
    )
    assert isinstance(replaced, SavedList)
    assert [song.url for song in replaced.songs] == [
        "https://example.com/0",
        "https://example.com/1",
        "https://example.com/2",
    ]


def test_a_bad_file_is_left_alone(tmp_path):
    """A bad session file is left alone, and an old line file moves once."""
    case_a_bad_session_file_is_left_alone()
    spot = tmp_path / "part1"
    spot.mkdir()
    case_an_old_music_line_file_moves_once(spot)

def case_a_bad_session_file_is_left_alone():
    import music.archive as archive

    archive.PATH.write_text("{not json", encoding="utf-8")
    archive._book = None
    archive._cache_path = None
    archive._failed_path = None
    assert archive.read_guild(5) is None
    assert archive.read_all() == {}
    archive.write_guild(5, {"current": {"title": "Kept"}})
    assert archive.PATH.read_text(encoding="utf-8") == "{not json"

    archive.PATH.write_text("[]\n", encoding="utf-8")
    archive._book = None
    archive._cache_path = None
    archive._failed_path = None
    archive.write_guild(5, {"current": {"title": "Kept"}})
    assert archive.PATH.read_text(encoding="utf-8") == "[]\n"


def case_an_old_music_line_file_moves_once(tmp_path):
    from music import archive

    legacy = tmp_path / "music_line.json"
    current = tmp_path / "music_session.json"
    legacy.write_text("{}\n", encoding="utf-8")
    archive._adopt_previous(current, "music_line.json")
    assert not legacy.exists()
    assert current.read_text(encoding="utf-8") == "{}\n"

    legacy.write_text('{"9": {}}\n', encoding="utf-8")
    archive._adopt_previous(current, "music_line.json")
    assert legacy.exists()
    assert current.read_text(encoding="utf-8") == "{}\n"
