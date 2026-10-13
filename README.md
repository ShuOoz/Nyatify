# Nyatify

A Discord bot that announces when someone starts an empty voice (or stage) channel. Features load as independent cogs, so you can run everything or isolate one function.

## What it does

When the first human joins an empty voice or stage channel, Nyatify posts an `@everyone` ping and then a silent embed card. The ping is its own message so phone and in-app alerts stay plain text; the card keeps a clickable channel and a live start time. Both are removed when that person leaves voice or goes AFK, or after 15 minutes. A short reconnect window ignores leave-and-rejoin spam.

Bots joining a room do not trigger a notice. AFK channels are ignored.

It can also post **game** / **ticket** / **other** cards from Bilibili or Weibo (the bot talks to both sites) and **match** cards from TheSportsDB. `/news add` asks for a **platform** first, then an account: pick TheSportsDB and type a club (nicknames and typos like `los angelos` suggest `Los Angeles Lakers · Basketball`). Club follows go to the **match** topic. Bilibili / Weibo stay **unassigned** until `/news feature` sets game, ticket, or other. Each fixture is **one Discord card** in three stages: preview (starting 6 hours before kickoff), match on (live score when known), then full time. The body is status, league · home, then kickoff time / period / recap. The card is deleted a day and a half after the result. Kickoff is Discord local time plus a relative countdown. The card links to the ESPN match page when that fixture is on ESPN’s scoreboard for the same day; otherwise it uses the TheSportsDB event page.

## Setup

1. Create a bot application in the [Discord Developer Portal](https://discord.com/developers/applications).
2. Enable these **Privileged Gateway Intents**: **Server Members** and **Message Content** (Message Content is only needed for `!ping`). Voice notifications also need the **Voice State** intent.
3. Invite the bot with permission to send messages (and mention `@everyone` if you want the default ping) in the channel you will use for announcements.
4. Copy `config.example.txt` to `config.txt` and set `DISCORD_TOKEN`. Environment variables override the file. `config.txt` is gitignored and should not be committed.

```text
DISCORD_TOKEN=your_bot_token_here
COMMAND_PREFIX=!
LOG_LEVEL=INFO
# Empty loads every cog. Example: voice
ENABLED_COGS=
# Server ID for instant /commands in that server only. Empty syncs globally (can take up to an hour).
SYNC_GUILD_ID=
# Empty: mint a Sina visitor SUB on first poll. Or paste SUB from m.weibo.cn.
WEIBO_COOKIES=
# Chat model host. Empty URL or model = no AI; news cards use the original excerpt.
# Full chat URL. Example: OpenAI https://api.openai.com/v1/chat/completions
# Background fetch. Match cards and Bilibili/Weibo posts are separate.
# Poll skips AI unless AI_ON_POLL=1 and a model is configured.
NEWS_POLL_MATCH=1
NEWS_POLL_POST=1
AI_API_KEY=
AI_BASE_URL=
AI_MODEL=
AI_ON_POLL=0
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

`/news add` takes a `space.bilibili.com` link or uid for Bilibili. Weibo takes a numeric uid: `weibo.com/u/…`, `m.weibo.cn/u/…`, or the number itself. A custom path such as `weibo.com/rmrb` needs the numeric id (`$CONFIG.oid` on the desktop profile).

Leave `/news cookie` empty to show status. Cookie values stay in `config.txt`. Put Bilibili's `ac_time_value` (localStorage on `https://www.bilibili.com`) on the end of **login**. Delete `BILI_COOKIE` or `WEIBO_COOKIES` to go back to a guest session. Sign out of the Bilibili website before the bot refreshes that login.

`NEWS_POLL_MATCH` and `NEWS_POLL_POST` enable background fetch (both on in `config.example.txt`). A Bilibili or Weibo follow waits until `/news feature` sets a topic. `/news add` says when poll is off. `/news check` previews a recent post or the club's next game and does not post. Background cards keep the original excerpt unless `AI_ON_POLL=1` and a model is configured.

Per-server settings are stored under `data/` (`guild.json` and announcement notice state). That directory is gitignored, except `data/sports_aliases.json` (club nicknames) and `data/esports_aliases.json` (team nicknames).

## Commands

`!ping` is the only prefix command (`COMMAND_PREFIX`, default `!`). Everything else is slash-only. Commands work in a server, not in DMs.

| Command | Who | What |
| --- | --- | --- |
| `/ping` or `!ping` | Anyone in the server | Check that the bot is online |
| `/announce show` | Manage Server | Show each function’s announcement channel (voice first, other last) |
| `/announce feature` | Manage Server | Default channel for every announcement |
| `/announce feature` with a function | Manage Server | Channel for one announcement function (`voice`, `game`, `ticket`, `match`, `other`) |
| `/news add` | Manage Server | Follow by platform, then account (`thesportsdb`, `bilibili`, `weibo`). TheSportsDB clubs go to `match`. Bilibili / Weibo stay unassigned until `/news feature` |
| `/news feature` | Manage Server | Set a followed account’s topic. Account first, then topic (`game`, `ticket`, `other`, or `match` for clubs) |
| `/news delete` | Manage Server | Unfollow a news account |
| `/news show` | Anyone in the server | Show followed accounts grouped by topic |
| `/news check` | Anyone in the server, while `/admin` has shown it | Preview a recent post, or a club's next game; does not post, and is not a replay of the next poll |
| `/admin` | Anyone in the server with `ADMIN_PASSWORD` | Show the owner commands below. Leave the password empty to hide them. A restart hides them too. With `SYNC_GUILD_ID`, that server’s list updates immediately |
| `/news cookie` | Bot owner, while `/admin` has shown it | Empty shows Bilibili and Weibo cookie status. Otherwise **platform** is `Bilibili` or `Weibo` and **login** is the Cookie header |
| `/cogs`, `/load`, `/unload`, `/reload` | Bot owner, while `/admin` has shown them | List or hot-load cogs |

`/admin` replies only to the person who ran it. The owner commands still require the bot owner.

`announce` and `notice` (shared delivery) are not cogs. Reload a cog such as `voice`; restart the bot to pick up `announce.py` or `notice.py` changes.

## Functions

Each `.py` file in `cogs/` (except names starting with `_`) is loaded as a discord.py extension.

| Cog | Role |
| --- | --- |
| `admin` | Owner cog management and per-server announce channels |
| `voice` | Voice/stage start notices (`cogs/voice_notify.py`) |
| `news` | Game/ticket/other feed cards (`cogs/news_notify.py`); TheSportsDB match card (preview → live → full time); Bilibili and Weibo in-process; optional AI filter and summary |

To add a notify function, subclass `announce.NotifyCog`, set `feature`, and post with `send_announcement`. To skip a file without deleting it, prefix the name with `_`.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```
