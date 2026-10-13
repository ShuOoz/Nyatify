from __future__ import annotations

import json
from pathlib import Path

import pytest

from guild import (
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


def test_guild_config(tmp_path, news_feature):
    case_legacy_guild_file_still_resolves_the_announce_channel(tmp_path)
    case_existing_guild_json_is_kept_when_the_legacy_file_remains(tmp_path)
    case_channels_persist_per_guild(tmp_path)
    case_feature_override_beats_default_until_cleared(tmp_path, news_feature)
    case_unregister_and_unknown_feature(tmp_path)
    case_guild_list_round_trip(tmp_path)
    case_unreadable_guild_file_is_not_overwritten(tmp_path)

def case_legacy_guild_file_still_resolves_the_announce_channel(tmp_path: Path):
    legacy = tmp_path / "guild_config.json"
    current = tmp_path / "guild.json"
    legacy.write_text(
        json.dumps({"guilds": {"1": {"announce_channel_id": 42}}}),
        encoding="utf-8",
    )
    store = GuildConfig(current)

    assert store.get_default_announce_channel_id(1) == 42
    assert not legacy.exists()
    assert json.loads(current.read_text(encoding="utf-8"))["guilds"]["1"]["announce_channel_id"] == 42
    assert GuildConfig(current).get_default_announce_channel_id(1) == 42


def case_existing_guild_json_is_kept_when_the_legacy_file_remains(tmp_path: Path):
    legacy = tmp_path / "guild_config.json"
    current = tmp_path / "guild.json"
    legacy.write_text(
        json.dumps({"guilds": {"1": {"announce_channel_id": 7}}}),
        encoding="utf-8",
    )
    current.write_text(
        json.dumps({"guilds": {"1": {"announce_channel_id": 99}}}),
        encoding="utf-8",
    )
    store = GuildConfig(current)
    assert store.get_default_announce_channel_id(1) == 99
    assert legacy.exists()
    assert GuildConfig(current).get_default_announce_channel_id(1) == 99


def case_channels_persist_per_guild(tmp_path: Path):
    path = tmp_path / "guild.json"
    store = GuildConfig(path)
    store.set_announce_channel_id(1, 10)
    store.set_announce_channel_id(2, 20)
    assert store.get_default_announce_channel_id(1) == 10
    store.remove_guild(1)
    assert store.get_default_announce_channel_id(1) is None
    assert store.get_default_announce_channel_id(2) == 20
    path.write_text('{"guilds": {}}\n', encoding="utf-8")
    assert store.get_default_announce_channel_id(2) == 20
    store.set_announce_channel_id(2, 30)
    assert GuildConfig(path).get_default_announce_channel_id(2) == 30


def case_feature_override_beats_default_until_cleared(tmp_path: Path, news_feature: str):
    store = GuildConfig(tmp_path / "guild.json")
    store.set_announce_channel_id(1, 10)
    store.set_feature_announce_channel_id(1, news_feature, 20)
    assert store.get_default_announce_channel_id(1) == 10
    assert store.get_feature_override_id(1, news_feature) == 20
    store.set_feature_announce_channel_id(1, "news_alias", 21)
    assert store.get_feature_overrides(1) == {news_feature: 21}
    assert canonical_announce_feature("cogs.news_alias") == news_feature
    store.clear_feature_announce_channel_id(1, news_feature)
    assert store.get_feature_override_id(1, news_feature) is None
    assert store.get_default_announce_channel_id(1) == 10
    store.set_feature_announce_channel_id(1, news_feature, 20)
    store.set_announce_channel_id(1, 30)
    assert store.get_feature_override_id(1, news_feature) is None
    assert store.get_default_announce_channel_id(1) == 30
    assert store.get_feature_overrides(1) == {}
    store.set_feature_announce_channel_id(1, news_feature, 20)
    cleared, features = store.forget_announce_channel(1, 20)
    assert cleared is False
    assert features == (news_feature,)
    assert store.get_feature_override_id(1, news_feature) is None
    assert store.get_default_announce_channel_id(1) == 30


def case_unregister_and_unknown_feature(tmp_path: Path):
    register_announce_feature("sample_fn", "sample_alias")
    assert canonical_announce_feature("sample_alias") == "sample_fn"
    unregister_announce_feature("sample_fn")
    assert canonical_announce_feature("sample_fn") is None
    assert canonical_announce_feature("sample_alias") is None
    store = GuildConfig(tmp_path / "guild.json")
    with pytest.raises(ValueError, match="Unknown announce function"):
        store.set_feature_announce_channel_id(1, "not_a_function", 20)


def case_guild_list_round_trip(tmp_path: Path):
    path = tmp_path / "guild.json"
    store = GuildConfig(path)
    store.set_announce_channel_id(1, 10)
    store.set_guild_list(1, "items", [{"a": 1}])
    assert store.guild_list(1, "items") == [{"a": 1}]
    store.set_guild_list(1, "items", [])
    other = GuildConfig(path)
    assert other.guild_list(1, "items") == []
    assert other.get_default_announce_channel_id(1) == 10


def case_unreadable_guild_file_is_not_overwritten(tmp_path: Path):
    path = tmp_path / "broken.json"
    path.write_text("{", encoding="utf-8")
    store = GuildConfig(path)
    store.set_announce_channel_id(1, 10)
    assert path.read_text(encoding="utf-8") == "{"
