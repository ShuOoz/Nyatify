from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import nyatify
from nyatify import Nyatify, bot_intents, hold_process_lock


def _bot_for_sync() -> Nyatify:
    bot = Nyatify(enabled_cogs=["admin"])
    bot.tree.copy_global_to = MagicMock()
    bot.tree.sync = AsyncMock(return_value=[object()])
    bot.tree.fetch_commands = AsyncMock(return_value=[])
    bot.tree.get_commands = MagicMock(return_value=[])
    bot.tree.clear_commands = MagicMock()
    bot.tree.add_command = MagicMock()
    return bot


async def _guilds(*guilds, **_kwargs):
    for guild in guilds:
        yield guild



async def test_intents_and_command_sync(monkeypatch):
    case_intents_follow_selected_cogs()
    await case_guild_sync_publishes_to_that_server_and_clears_others(monkeypatch)
    await case_global_sync_clears_leftover_guild_commands(monkeypatch)

def case_intents_follow_selected_cogs():
    all_intents = bot_intents(None)
    assert all_intents.voice_states is True
    assert all_intents.message_content is True
    assert all_intents.members is True
    assert all_intents.dm_messages is False
    admin = bot_intents(["admin"])
    assert admin.voice_states is False
    assert admin.message_content is True
    voice = bot_intents(["voice"])
    assert voice.voice_states is True
    assert voice.message_content is False


async def case_guild_sync_publishes_to_that_server_and_clears_others(monkeypatch):
    monkeypatch.setattr("nyatify.settings.sync_guild_id", 111)
    bot = _bot_for_sync()
    keep = MagicMock()
    keep.id = 111
    other = MagicMock()
    other.id = 222
    leftover = object()
    local = object()
    bot.fetch_guilds = lambda **_kwargs: _guilds(keep, other)
    bot.tree.fetch_commands = AsyncMock(return_value=[leftover])
    bot.tree.get_commands = MagicMock(return_value=[local])
    await bot._sync_slash_commands()
    await bot.close()
    bot.tree.copy_global_to.assert_called_once()
    assert bot.tree.copy_global_to.call_args.kwargs["guild"].id == 111
    assert bot.tree.clear_commands.call_args_list[0].kwargs["guild"].id == 111
    synced = [call.kwargs.get("guild") for call in bot.tree.sync.await_args_list]
    assert synced[0].id == 111
    assert synced[1] is None
    assert synced[2] is other
    bot.tree.clear_commands.assert_any_call(guild=None)
    bot.tree.clear_commands.assert_any_call(guild=other)
    assert bot.tree.add_command.call_count == 2


async def case_global_sync_clears_leftover_guild_commands(monkeypatch):
    monkeypatch.setattr("nyatify.settings.sync_guild_id", None)
    bot = _bot_for_sync()
    other = MagicMock()
    other.id = 222
    leftover = object()
    bot.fetch_guilds = lambda **_kwargs: _guilds(other)
    bot.tree.fetch_commands = AsyncMock(
        side_effect=lambda *, guild=None: [leftover] if guild is other else []
    )
    await bot._sync_slash_commands()
    await bot.close()
    bot.tree.copy_global_to.assert_not_called()
    assert [call.kwargs.get("guild") for call in bot.tree.sync.await_args_list] == [
        None,
        other,
    ]
    bot.tree.clear_commands.assert_called_once_with(guild=other)

    empty = _bot_for_sync()
    empty.fetch_guilds = lambda **_kwargs: _guilds(other)
    await empty._sync_slash_commands()
    await empty.close()
    assert [call.kwargs.get("guild") for call in empty.tree.sync.await_args_list] == [None]
    empty.tree.clear_commands.assert_not_called()


def _drop_lock() -> None:
    handle = nyatify._lock_file
    nyatify._lock_file = None
    if handle is not None:
        handle.close()


def _second_start(path: Path) -> subprocess.CompletedProcess[str]:
    root = str(Path(__file__).resolve().parents[1])
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "from nyatify import hold_process_lock\n"
        "hold_process_lock(Path(sys.argv[1]))\n"
    )
    return subprocess.run(
        [sys.executable, "-c", code, str(path)],
        cwd=root,
        env={**os.environ, "PYTHONPATH": root},
        capture_output=True,
        text=True,
        timeout=30,
    )


def _run_main(path: Path) -> subprocess.CompletedProcess[str]:
    """Start like the real entry point, but stop inside login."""
    root = str(Path(__file__).resolve().parents[1])
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "import nyatify\n"
        "nyatify.configure_logging = lambda: None\n"
        "nyatify._LOCK_PATH = Path(sys.argv[1])\n"
        "nyatify.settings.require_token = lambda: 'token'\n"
        "def run(self, token, log_handler=None, **kwargs):\n"
        "    if nyatify._lock_file is None:\n"
        "        raise SystemExit(2)\n"
        "    print('RUN', flush=True)\n"
        "nyatify.Nyatify.run = run\n"
        "nyatify.main()\n"
    )
    return subprocess.run(
        [sys.executable, "-c", code, str(path)],
        cwd=root,
        env={**os.environ, "PYTHONPATH": root},
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_one_process_lock(tmp_path):
    case_a_second_start_exits(tmp_path)
    case_the_next_start_runs_after_this_process_exits(tmp_path)
    case_main_does_not_log_in_when_a_copy_is_running(tmp_path)
    case_main_logs_in_only_while_holding_the_lock(tmp_path)


def case_a_second_start_exits(tmp_path):
    path = tmp_path / "nyatify.lock"
    hold_process_lock(path)
    try:
        child = _second_start(path)
    finally:
        _drop_lock()
    assert child.returncode == 1
    assert "already running" in child.stderr
    assert "Traceback" not in child.stderr


def case_the_next_start_runs_after_this_process_exits(tmp_path):
    path = tmp_path / "nyatify.lock"
    hold_process_lock(path)
    _drop_lock()
    child = _second_start(path)
    assert child.returncode == 0
    assert child.stderr == ""


def case_main_does_not_log_in_when_a_copy_is_running(tmp_path):
    path = tmp_path / "main.lock"
    hold_process_lock(path)
    try:
        child = _run_main(path)
    finally:
        _drop_lock()
    assert child.returncode == 1
    assert "RUN" not in child.stdout
    assert "already running" in child.stderr
    assert "Traceback" not in child.stderr


def case_main_logs_in_only_while_holding_the_lock(tmp_path):
    path = tmp_path / "main.lock"
    child = _run_main(path)
    assert child.returncode == 0, child.stderr
    assert child.stdout.strip() == "RUN"
