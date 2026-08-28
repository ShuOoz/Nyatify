from __future__ import annotations

from pathlib import Path

import pytest

from guild_config import (
    GuildConfig,
    canonical_announce_feature,
    register_announce_feature,
    unregister_announce_feature,
)

NEWS = "news"


@pytest.fixture
def news_feature():
    register_announce_feature(NEWS, "news_alias")
    return NEWS


def test_set_and_get_are_per_guild(tmp_path: Path):
    store = GuildConfig(tmp_path / "guild_config.json")
    store.set_announce_channel_id(1, 10)
    store.set_announce_channel_id(2, 20)

    assert store.get_announce_channel_id(1) == 10
    assert store.get_announce_channel_id(2) == 20

    store.remove_guild(1)
    assert store.get_announce_channel_id(1) is None
    assert store.get_announce_channel_id(2) == 20


def test_reload_from_disk(tmp_path: Path):
    path = tmp_path / "guild_config.json"
    first = GuildConfig(path)
    first.set_announce_channel_id(3, 99)
    second = GuildConfig(path)
    assert second.get_announce_channel_id(3) == 99


def test_reads_keep_using_memory_until_this_store_writes(tmp_path: Path):
    path = tmp_path / "guild_config.json"
    store = GuildConfig(path)
    store.set_announce_channel_id(1, 10)
    path.write_text('{"guilds": {}}\n', encoding="utf-8")

    assert store.get_announce_channel_id(1) == 10

    store.set_announce_channel_id(1, 20)
    assert store.get_announce_channel_id(1) == 20
    other = GuildConfig(path)
    assert other.get_announce_channel_id(1) == 20


def test_feature_override_beats_default(tmp_path: Path, news_feature: str):
    store = GuildConfig(tmp_path / "guild_config.json")
    store.set_announce_channel_id(1, 10)
    store.set_feature_announce_channel_id(1, news_feature, 20)

    assert store.get_announce_channel_id(1) == 10
    assert store.get_feature_override_id(1, news_feature) == 20
    assert store.get_announce_channel_id(1, news_feature) == 20
    assert store.get_feature_overrides(1) == {news_feature: 20}


def test_set_all_clears_feature_overrides(tmp_path: Path, news_feature: str):
    store = GuildConfig(tmp_path / "guild_config.json")
    store.set_announce_channel_id(1, 10)
    store.set_feature_announce_channel_id(1, news_feature, 20)
    store.set_announce_channel_id(1, 30)

    assert store.get_announce_channel_id(1, news_feature) == 30
    assert store.get_feature_overrides(1) == {}


def test_clear_feature_falls_back_to_default(tmp_path: Path, news_feature: str):
    store = GuildConfig(tmp_path / "guild_config.json")
    store.set_announce_channel_id(1, 10)
    store.set_feature_announce_channel_id(1, news_feature, 20)
    store.clear_feature_announce_channel_id(1, news_feature)

    assert store.get_announce_channel_id(1, news_feature) == 10


def test_alias_stores_as_canonical_name(tmp_path: Path, news_feature: str):
    store = GuildConfig(tmp_path / "guild_config.json")
    store.set_feature_announce_channel_id(1, "news_alias", 20)
    assert store.get_feature_overrides(1) == {news_feature: 20}
    assert store.get_announce_channel_id(1, "news_alias") == 20
    assert canonical_announce_feature("cogs.news_alias") == news_feature


def test_unregister_drops_canonical_and_aliases():
    register_announce_feature("sample_fn", "sample_alias")
    assert canonical_announce_feature("sample_alias") == "sample_fn"

    unregister_announce_feature("sample_fn")
    assert canonical_announce_feature("sample_fn") is None
    assert canonical_announce_feature("sample_alias") is None


def test_unknown_feature_cannot_set_an_override(tmp_path: Path):
    store = GuildConfig(tmp_path / "guild_config.json")
    with pytest.raises(ValueError, match="Unknown announce function"):
        store.set_feature_announce_channel_id(1, "not_a_function", 20)


def test_forget_deleted_feature_channel_uses_default(tmp_path: Path, news_feature: str):
    store = GuildConfig(tmp_path / "guild_config.json")
    store.set_announce_channel_id(1, 10)
    store.set_feature_announce_channel_id(1, news_feature, 20)

    default_cleared, features = store.forget_announce_channel(1, 20)

    assert default_cleared is False
    assert features == (news_feature,)
    assert store.get_feature_overrides(1) == {}
    assert store.get_announce_channel_id(1, news_feature) == 10
