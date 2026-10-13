from __future__ import annotations

import pytest

from ai import AI
from cogs.news_notify import NewsNotify
from guild import (
    GuildConfig,
    register_announce_feature,
    unregister_announce_feature,
)
from news.follows import NEWS_FEATURES
from tests.helpers import (
    ACCOUNT,
    GUILD_ID,
    NOTIFY_CHANNEL_ID,
    RoundRig,
    ScriptedFetcher,
    fake_bot,
    fake_guild,
    fake_text_channel,
)

__all__ = ["ACCOUNT", "GUILD_ID", "NOTIFY_CHANNEL_ID"]


def fresh_state(base):
    """Every saved file the round modules keep, one fresh set under `base`. Returns the undo."""
    import news.source.post.bilibili.app as bili_app
    import news.source.post.bilibili.round as bili_round
    import news.source.post.common as common
    from news.source.post.bilibili.app import PhoneBook
    from news.source.post.bilibili.round import BlankPlan, Rotation
    from news.source.post.common import ExitBook, weibo_rest
    from news.source.post.weibo.http import _lanes, guests

    def swap(module, name, value):
        previous = getattr(module, name)
        setattr(module, name, value)
        return module, name, previous

    def point(book, path, cache):
        previous = (book.path, getattr(book, cache))
        book.path = path
        setattr(book, cache, None)
        return book, path, cache, previous

    _lanes.clear()
    undo = (
        swap(common, "exits", ExitBook(base / "proxy_exits.json")),
        swap(bili_round, "rotation", Rotation(base / "bili_rotation.json")),
        swap(bili_round, "cap", common.ProxyRest(base / "bili_empty_cap.json")),
        swap(bili_round, "blanks", BlankPlan(base / "bili_blank.json")),
        swap(bili_app, "buvid_store", PhoneBook(base / "bili_phones.json")),
        point(weibo_rest, base / "weibo_rest.json", "_out"),
        point(guests, base / "weibo_guests.json", "_rows"),
    )

    def restore() -> None:
        _lanes.clear()
        for module, name, previous in undo[:5]:
            setattr(module, name, previous)
        for book, _path, cache, (path, rows) in undo[5:]:
            book.path = path
            setattr(book, cache, rows)

    return restore


@pytest.fixture(autouse=True)
def _fresh_state(tmp_path):
    """Saved exits, phones, sit-outs and guests stay in the test tmp dir."""
    restore = fresh_state(tmp_path)
    yield
    restore()


@pytest.fixture
def fresh(tmp_path):
    """Call between scenarios of one test to start from clean saved state."""
    undos = []

    def again():
        base = tmp_path / f"scene{len(undos)}"
        base.mkdir()
        undos.append(fresh_state(base))
        return base

    yield again
    for undo in reversed(undos):
        undo()


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch):
    """A proxy in config.txt must not reach test HTTP."""
    from config import settings as live_settings

    monkeypatch.setattr(live_settings, "bili_proxy", None)
    monkeypatch.setattr(live_settings, "bili_proxies", ())
    monkeypatch.setattr(live_settings, "bili_phones_per_proxy", 1)
    monkeypatch.setattr(live_settings, "bili_accounts_per_phone", 2)
    monkeypatch.setattr(live_settings, "weibo_proxy", None)
    monkeypatch.setattr(live_settings, "weibo_proxies", ())
    monkeypatch.setattr(live_settings, "ai_proxy", None)
    monkeypatch.setattr(live_settings, "ip_check", False)


@pytest.fixture
def news_store(tmp_path):
    config = GuildConfig(tmp_path / "guild.json")
    config.set_announce_channel_id(GUILD_ID, NOTIFY_CHANNEL_ID)
    return config


@pytest.fixture
def news_guild():
    return fake_guild(GUILD_ID)


@pytest.fixture
def news_channel(news_guild):
    channel = fake_text_channel(NOTIFY_CHANNEL_ID, guild=news_guild)
    news_guild.get_channel.return_value = channel
    return channel


@pytest.fixture
def news_cog(news_store, tmp_path, monkeypatch):
    from config import settings as live_settings

    monkeypatch.setattr(live_settings, "bili_cookie", None)
    monkeypatch.setattr(live_settings, "bili_ac_time", None)

    for name in NEWS_FEATURES:
        register_announce_feature(name)
    notify = NewsNotify(
        fake_bot(),
        guild_config=news_store,
        fetcher=ScriptedFetcher(),
        ai=AI(),
        ai_on_poll=False,
        notices_path=tmp_path / "news_notices.json",
        start_polling=False,
    )
    notify._post_fetch_gap = 0
    notify._ticket_fetch_gap = 0
    notify._damai_fetch_gap = 0
    notify._match_fetch_gap = 0
    yield notify
    notify.notices.stop(force=True)
    for name in NEWS_FEATURES:
        unregister_announce_feature(name)


@pytest.fixture
def rig(news_store, monkeypatch):
    """A poll's pieces without a cog, for the round modules."""
    from config import settings as live_settings

    monkeypatch.setattr(live_settings, "bili_cookie", None)
    monkeypatch.setattr(live_settings, "bili_ac_time", None)
    return RoundRig(news_store)


@pytest.fixture
def scene(fresh, monkeypatch):
    """Call for a clean rig and clean saved state, once per scenario of a test."""
    from config import settings as live_settings

    monkeypatch.setattr(live_settings, "bili_cookie", None)
    monkeypatch.setattr(live_settings, "bili_ac_time", None)

    def make() -> RoundRig:
        config = GuildConfig(fresh() / "guild.json")
        config.set_announce_channel_id(GUILD_ID, NOTIFY_CHANNEL_ID)
        return RoundRig(config)

    return make
