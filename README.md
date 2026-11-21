<div align="center">

<img src="image/nyatify_icon.png" alt="Nyatify" width="120"/>

# Nyatify

**A Discord bot that lets you and your friends feel more connected**

<p>
  <a href="https://www.python.org/downloads/"><img alt="Python 3.11+" src="image/docs_image/readme/badge_python.png" /></a>
  <a href="https://github.com/Rapptz/discord.py"><img alt="discord.py 2.3+" src="image/docs_image/readme/badge_discord.png" /></a>
  <a href="https://docs.pytest.org/"><img alt="pytest tested" src="image/docs_image/readme/badge_pytest.png" /></a>
</p>

<a href="https://techforpalestine.org/learn-more"><img alt="Ceasefire Now" src="image/docs_image/readme/badge_ceasefire.png" /></a>

<a href="https://ko-fi.com/shuoy" target="_blank"><img height="48" src="image/docs_image/readme/kofi.png" alt="Buy Me a Coffee at ko-fi.com" /></a>

English | [简体中文](README.CN.md)

</div>

## ✨ Introduction

Nyatify is a Discord bot for voice announcements, music, and followed posts.

- 🔊 **Voice**: A message when someone starts an empty voice or stage channel.
- 🎵 **Music**: Play a song in a voice channel.
- 🏆 **Match**: Follow a sports club or an esports team.
- 🎫 **Game, ticket, and other**: Subscribe to a **Bilibili** or **Weibo** account under a topic.

## 🚀 Installation

**📋 Requirement:**

- Python 3.11+
- Docker

**1️⃣ Clone the repository:**

```bash
git clone https://github.com/ShuOoz/Nyatify.git
cd Nyatify
```

**2️⃣ Install dependencies:**

With Python:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

With Docker:

```bash
docker build -t nyatify .
docker run --rm -e DISCORD_TOKEN=your_bot_token_here nyatify
```

**3️⃣ Configure environment:**

1. [🎮 Create a bot in the Discord Developer Portal.](https://discord.com/developers/applications)
2. On the Bot page, enable Server Members Intent and Message Content Intent.
3. On OAuth2 or Guild Install, check `bot` and `applications.commands`.
4. Bot Permissions: Send Messages, Embed Links, Attach Files, Mention Everyone, Connect, Speak.
5. Use the Generated URL in the portal to install the bot.

**4️⃣ Start the bot:**

```bash
python nyatify.py
```

**🧪 Tests:**

```bash
pip install -r requirements-dev.txt
pytest
```

## ⚙️ Configuration

Nyatify reads `config.txt`. Copy `config.example.txt` to `config.txt` and set `DISCORD_TOKEN`.

### 🔐 Discord

```bash
# The quoted text is an example of what to type.
# In config.txt, write the value without quotes.
# Token from the Discord Developer Portal.
DISCORD_TOKEN="YOUR_BOT_TOKEN_HERE"

COMMAND_PREFIX="!"
LOG_LEVEL="INFO"
ENABLED_COGS="voice"  # Empty loads every cog.

# Server ID. Empty syncs every server, which can take up to an hour.
SYNC_GUILD_ID="123456789012345678"

# Empty hides /admin. A restart hides the owner commands too.
ADMIN_PASSWORD="YOUR_PASSWORD"
```

### 🏆 Match

```bash
NEWS_POLL_MATCH="1"
THESPORTSDB_KEY="YOUR_THESPORTSDB_KEY"    # Empty uses the free test key.
PANDASCORE_TOKEN="YOUR_PANDASCORE_TOKEN"  # Empty skips PandaScore.
```

### 🎫 Bilibili and Weibo

```bash
NEWS_POLL_POST="1"

# Cookie header from bilibili.com. Empty keeps a guest session.
BILI_COOKIE="SESSDATA=...; bili_jct=...; DedeUserID=123"

# ac_time_value from localStorage on bilibili.com.
BILI_AC_TIME="YOUR_AC_TIME_VALUE"

BILI_COOKIE_UID="0"                 # 0 means no Bilibili login is saved.
WEIBO_COOKIES="SUB=YOUR_WEIBO_SUB"  # Empty mints a guest SUB on the next poll.
```

### 🌐 Proxies

```bash
BILI_PROXY="http://127.0.0.1:7890,http://127.0.0.1:7891"  # Empty connects directly.

# Empty connects directly. A guest stays on the proxy that created it.
WEIBO_PROXY="http://127.0.0.1:7890"

AI_PROXY="http://127.0.0.1:7890"  # Empty connects directly.
BILI_PHONES_PER_PROXY="1"
BILI_ACCOUNTS_PER_PHONE="2"
IP_CHECK="1"  # 0 skips the exit-IP check.
```

### 🤖 AI

```bash
AI_API_KEY="YOUR_AI_API_KEY"  # Empty key, URL, or model means no AI.

# Full chat URL, not the site root.
AI_BASE_URL="https://api.deepseek.com/chat/completions"

AI_MODEL="deepseek-flash"
AI_ON_POLL="0"  # 1 runs the model on background polls.
```

### 🎵 Music

```bash
# Lavalink node. Empty leaves play unable to start.
# Example URI: http://127.0.0.1:2333
LAVALINK_URI="http://127.0.0.1:2333"
LAVALINK_PASSWORD="YOUR_LAVALINK_PASSWORD"

# NodeLink is the NetEase node. Empty keeps NetEase on the Java node.
NODELINK_URI="http://127.0.0.1:2334"
NODELINK_PASSWORD="YOUR_NODELINK_PASSWORD"

# Optional. Radio asks Last.fm for a similar song.
# Empty keeps searching the current title. https://www.last.fm/api/account/create
LASTFM_API_KEY="YOUR_LASTFM_API_KEY"
```

## 🧩 Cogs

[Voice](docs/voice.md), [Music](docs/music.md), and [News](docs/news.md) have their own pages for how they work and their commands.

| Cog | Role |
| --- | --- |
| 🔐 Admin | Owner cog management and per-server announce channels. |
| 🔊 Voice | Voice and stage start notices. |
| 🎵 Music | Play songs in a voice channel. |
| 📰 News | Match cards, plus Bilibili and Weibo posts. |

Shared commands and owner commands are listed here.

| Command | Description |
| --- | --- |
| `!ping` | Check that the bot is online. |
| `/announce show` | Show announcement channels for this server. |
| `/announce feature` | Set the announcement channel for this server. |
| `/admin` | Show or hide the owner commands. |
| `/cogs` | List loaded and available cogs. Owner only. |
| `/load` | Load one cog without restarting the bot. Owner only. |
| `/unload` | Unload one cog without restarting the bot. Owner only. |
| `/reload` | Reload one cog without restarting the bot. Owner only. |

## ⚠️ Limitation

🔐 **Admin**

- 🙈 **Owner commands**: Stay hidden until `/admin`. A restart hides them again.
- 🔒 **Unload**: The admin cog cannot be unloaded.
- 💬 **Direct messages**: Commands run in a server, not in a direct message.

🔊 **Voice**

- 🚪 **Empty channel**: A notice starts only when a person joins an empty voice or stage channel.
- 🤖 **Bots and AFK**: Bots do not start a notice, and the AFK channel is ignored.
- ⏱️ **One notice**: The message is removed after 15 minutes, or when they leave or go AFK.

🎵 **Music**

- 🎧 **Voice**: Join a voice channel first.
- 🔗 **Sources**: <u>Apple Music, YouTube, Spotify, SoundCloud, and NetEase.</u>
- 🖥️ **Lavalink**: Play needs a Lavalink node in `config.txt`.
- ⏸️ **Idle**: No song, or a paused one, for five minutes leaves the channel. An empty room stays.
- 📻 **Radio**: Needs a Last.fm API key.

📰 **News**

- 🎫 **Posts**: <u>Bilibili and Weibo only.</u>
- 🔇 **Topic**: The topic is fixed by default; customize the filter and the rest.
- 🕐 **Kickoff**: A match card appears from 6 hours before kickoff, and not before that.
- 📅 **Quiet days**: With no match in that window, the match poll checks about once a day.
- 🗑️ **Full time**: The match card is removed a day and a half after full time.

## 🙏 Special Thanks

These are the people and projects that helped me a lot while building this bot.

[DIYgod](https://github.com/DIYgod), [dataabc](https://github.com/dataabc), [SocialSisterYi](https://github.com/SocialSisterYi), [Nemo2011](https://github.com/Nemo2011), [appujet](https://github.com/appujet), [AjieDev](https://github.com/AjieDev)

- [RSSHub](https://github.com/DIYgod/RSSHub): Feeds from many sites, including Bilibili and Weibo.
- [weibo-crawler](https://github.com/dataabc/weibo-crawler): A Python crawler for Sina Weibo.
- [LavaMusic](https://github.com/appujet/lavamusic): A Lavalink Discord music bot.
- [Free-Lavalink](https://github.com/AjieDev/Free-Lavalink): Public Lavalink servers.

🙇‍♂️🙇‍♂️🙇‍♂️ Big Thanks to [Cursor](https://cursor.com) 🙇‍♂️🙇‍♂️🙇‍♂️

## 📝 Disclaimer

Nyatify is a personal project for **learning and practice**. Use it within the **terms** of the services it reads.

Do not place **excessive load** on Bilibili, Weibo, or the match providers.

Do not send notices to people who have **not requested** them.
