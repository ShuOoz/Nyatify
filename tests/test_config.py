from __future__ import annotations

from pathlib import Path

import pytest

from config import (
    Settings,
    upsert_config_values,
    write_text_atomic,
)


def _settings(tmp_path: Path, text: str = "", environ: dict[str, str] | None = None) -> Settings:
    path = tmp_path / "config.txt"
    path.write_text(text, encoding="utf-8")
    return Settings(environ=environ or {}, config_path=path)



def test_settings_file(tmp_path, monkeypatch):
    case_settings_read_file_env_and_defaults(tmp_path)
    case_missing_token_raises(tmp_path)
    case_upsert_config_values_keeps_other_keys(tmp_path)
    case_write_text_atomic_replaces_or_keeps_old_file(tmp_path, monkeypatch)

def case_settings_read_file_env_and_defaults(tmp_path: Path):
    blank = _settings(tmp_path)
    assert blank.enabled_cogs is None
    assert blank.sync_guild_id is None
    assert blank.admin_password is None
    assert blank.news_poll is False
    assert blank.news_poll_match is False
    assert blank.news_poll_post is False
    assert blank.ai_on_poll is False
    assert blank.weibo_cookie is None
    assert blank.bili_proxy is None
    assert blank.bili_proxies == ()
    assert blank.bili_phones_per_proxy == 1
    assert blank.bili_accounts_per_phone == 2
    assert blank.ip_check is True
    assert blank.weibo_proxy is None
    assert blank.weibo_proxies == ()
    assert blank.ai_proxy is None
    assert blank.ai_api_key is None
    proxied = _settings(tmp_path, "BILI_PROXY= http://127.0.0.1:7890 \n")
    assert proxied.bili_proxy == "http://127.0.0.1:7890"
    assert proxied.bili_proxies == ("http://127.0.0.1:7890",)
    listed = _settings(tmp_path, "BILI_PROXY=http://a:1, http://b:2, http://a:1\n")
    assert listed.bili_proxies == ("http://a:1", "http://b:2")
    assert listed.bili_proxy == "http://a:1"
    sized = _settings(tmp_path, "BILI_PHONES_PER_PROXY=2\nBILI_ACCOUNTS_PER_PHONE=4\n")
    assert sized.bili_phones_per_proxy == 2
    assert sized.bili_accounts_per_phone == 4
    assert _settings(tmp_path, "IP_CHECK=0\n").ip_check is False
    assert _settings(tmp_path, "IP_CHECK=1\n").ip_check is True
    over = _settings(
        tmp_path,
        "BILI_ACCOUNTS_PER_PHONE=4\n",
        {"BILI_ACCOUNTS_PER_PHONE": "3"},
    )
    assert over.bili_accounts_per_phone == 3
    with pytest.raises(RuntimeError, match="BILI_PHONES_PER_PROXY"):
        _settings(tmp_path, "BILI_PHONES_PER_PROXY=0\n")
    assert proxied.weibo_proxy is None
    assert proxied.ai_proxy is None
    env_proxy = _settings(
        tmp_path,
        "BILI_PROXY=http://file:1\nWEIBO_PROXY=http://file:2\nAI_PROXY=http://file:3\n",
        {"BILI_PROXY": "http://env:2"},
    )
    assert env_proxy.bili_proxy == "http://env:2"
    assert env_proxy.bili_proxies == ("http://env:2",)
    assert env_proxy.weibo_proxy == "http://file:2"
    assert env_proxy.weibo_proxies == ("http://file:2",)
    listed_weibo = _settings(tmp_path, "WEIBO_PROXY=http://w0, http://w1, http://w0\n")
    assert listed_weibo.weibo_proxies == ("http://w0", "http://w1")
    assert listed_weibo.weibo_proxy == "http://w0"
    assert env_proxy.ai_proxy == "http://file:3"
    env = _settings(
        tmp_path,
        "DISCORD_TOKEN=file-token\nCOMMAND_PREFIX=?\nAI_API_KEY=file-key\n",
        {"DISCORD_TOKEN": "env-token", "COMMAND_PREFIX": "!", "AI_API_KEY": "env-key"},
    )
    assert env.token == "env-token"
    assert env.command_prefix == "!"
    assert env.ai_api_key == "env-key"
    parsed = _settings(
        tmp_path,
        "ENABLED_COGS=voice_notify, admin\n"
        "SYNC_GUILD_ID=123456789012345678\n"
        "ADMIN_PASSWORD=gate-pass-9\n"
        "BILI_COOKIE_UID=123\n"
        "BILI_AC_TIME=refresh-token\n"
        "WEIBO_COOKIES=SUB=login\n"
        "AI_BASE_URL=https://ai.example.test/chat/\n"
        "AI_MODEL=local-chat\n"
        "NEWS_POLL_MATCH=1\nNEWS_POLL_POST=1\nAI_ON_POLL=1\n",
    )
    assert parsed.enabled_cogs == ["voice_notify", "admin"]
    assert parsed.sync_guild_id == 123456789012345678
    assert parsed.admin_password == "gate-pass-9"
    assert parsed.bili_cookie_uid == "123"
    assert parsed.bili_ac_time == "refresh-token"
    assert parsed.weibo_cookie == "SUB=login"
    assert parsed.ai_base_url == "https://ai.example.test/chat"
    assert parsed.ai_model == "local-chat"
    assert parsed.news_poll is True
    assert parsed.news_poll_match is True
    assert parsed.news_poll_post is True
    assert parsed.ai_on_poll is True
    off = _settings(tmp_path, "NEWS_POLL_MATCH=0\nNEWS_POLL_POST=0\nAI_ON_POLL=0\n")
    assert off.news_poll is False
    match_only = _settings(tmp_path, "NEWS_POLL_MATCH=1\nNEWS_POLL_POST=0\n")
    assert match_only.news_poll is True
    assert match_only.news_poll_match is True
    assert match_only.news_poll_post is False
    post_only = _settings(tmp_path, "NEWS_POLL_POST=1\n")
    assert post_only.news_poll_match is False
    assert post_only.news_poll_post is True


def case_missing_token_raises(tmp_path: Path):
    with pytest.raises(RuntimeError, match="DISCORD_TOKEN"):
        _settings(tmp_path).require_token()


def case_write_text_atomic_replaces_or_keeps_old_file(tmp_path: Path, monkeypatch):
    path = tmp_path / "nested" / "data.json"
    path.parent.mkdir()
    path.write_text("old\n", encoding="utf-8")
    write_text_atomic(path, "new\n")
    assert path.read_text(encoding="utf-8") == "new\n"
    assert not path.with_name(f"{path.name}.tmp").exists()

    def boom(_self, _target):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", boom)
    with pytest.raises(OSError):
        write_text_atomic(path, "newer\n")
    assert path.read_text(encoding="utf-8") == "new\n"


def case_upsert_config_values_keeps_other_keys(tmp_path: Path):
    path = tmp_path / "config.txt"
    path.write_text("DISCORD_TOKEN=keep\nAI_MODEL=old\n", encoding="utf-8")
    upsert_config_values(path, {"AI_MODEL": "new"})
    text = path.read_text(encoding="utf-8")
    assert "DISCORD_TOKEN=keep" in text
    assert "AI_MODEL=new" in text
