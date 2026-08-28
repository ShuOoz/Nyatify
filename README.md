# Nyatify

A Discord bot that announces when someone starts an empty voice (or stage) channel. Features load as independent cogs, so you can run everything or isolate one function.

## What it does

When the first human joins an empty voice or stage channel, Nyatify posts an `@everyone` ping and then a silent embed card. The ping is its own message so phone and in-app alerts stay plain text; the card keeps a clickable channel and a live start time. Both are removed when that person leaves voice or goes AFK, or after 15 minutes. A short reconnect window ignores leave-and-rejoin spam.

Bots joining a room do not trigger a notice. AFK channels are ignored.

## Setup

1. Create a bot application in the [Discord Developer Portal](https://discord.com/developers/applications).
2. Enable these **Privileged Gateway Intents**: **Server Members** and **Message Content**. Voice notifications also need the **Voice State** intent.
3. Invite the bot with permission to send messages (and mention `@everyone` if you want the default ping) in the channel you will use for announcements.
4. Copy `config.example.txt` to `config.txt` and set `DISCORD_TOKEN`. Environment variables override the file. `config.txt` is gitignored and should not be committed.

```text
DISCORD_TOKEN=your_bot_token_here
COMMAND_PREFIX=!
LOG_LEVEL=INFO
# Empty loads every cog. Example: voice
ENABLED_COGS=
# Server ID so /commands appear immediately. Empty syncs globally (can take up to an hour).
SYNC_GUILD_ID=
```

Python 3.11+ is recommended (the Docker image uses 3.11).

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python nyatify.py
```

Or with Docker:

```bash
docker build -t nyatify .
docker run --rm -e DISCORD_TOKEN=your_bot_token_here nyatify
```

Per-server settings are stored under `data/` (`guild_config.json` and announcement notice state). That directory is gitignored.

## Commands

Prefix commands use `COMMAND_PREFIX` (default `!`). Slash commands are the same names.

| Command | Who | What |
| --- | --- | --- |
| `/ping` or `!ping` | Anyone | Check that the bot is online |
| `/announce show` or `!announce` | Manage Server | Show announcement channels |
| `/announce feature` or `!announce #channel` | Manage Server | Default channel for every cog |
| `/announce feature` with a cog, or `!announce voice #channel` | Manage Server | Channel for one cog only |
| `/cogs`, `/load`, `/unload`, `/reload` (and `!` equivalents) | Bot owner | List or hot-load cogs |

`announce` (the shared delivery module) is not a cog. Reload a cog such as `voice`; restart the bot to pick up `announce.py` changes.

## Functions

Each `.py` file in `cogs/` (except names starting with `_`) is loaded as a discord.py extension.

| Cog | Role |
| --- | --- |
| `admin` | Owner cog management and per-server announce channels |
| `voice` | Voice/stage start notices (`cogs/voice_notify.py`) |

To add a notify function, subclass `announce.NotifyCog`, set `feature`, and post with `send_announcement`. To skip a file without deleting it, prefix the name with `_`.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```
