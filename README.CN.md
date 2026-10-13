<div align="center">

<img src="image/nyatify_icon.png" alt="Nyatify" width="120"/>

# Nyatify

**一个让你和朋友们更亲近的 Discord 机器人**

<p>
  <a href="https://www.python.org/downloads/"><img alt="Python 3.11+" src="image/docs_image/badge-python.png" /></a>
  <a href="https://github.com/Rapptz/discord.py"><img alt="discord.py 2.3+" src="image/docs_image/badge-discord.png" /></a>
  <a href="https://docs.pytest.org/"><img alt="pytest 测过" src="image/docs_image/badge-pytest.png" /></a>
</p>

<a href="https://techforpalestine.org/learn-more"><img alt="Ceasefire Now" src="image/docs_image/badge-ceasefire.png" /></a>

<a href="https://ko-fi.com/shuoy" target="_blank"><img height="48" src="image/docs_image/kofi.png" alt="在 ko-fi.com 请我喝一杯" /></a>

[English](README.md) | 简体中文

</div>

## ✨ 介绍

Nyatify 是一个 Discord 机器人，会发语音通知，也会推送你关注的动态。

<br>

- 🔊 **语音**：有人加入一个空着的语音频道或舞台频道时，发一条消息。
- 🏆 **比赛**：关注一支体育俱乐部或电竞战队。
- 🎫 **游戏、门票和其他**：按话题订阅一个**哔哩哔哩**或**微博**账号。

## 🚀 安装

**📋 准备：**

- Python 3.11+
- Docker

**1️⃣ 克隆仓库：**

```bash
git clone https://github.com/ShuOoz/Nyatify.git
cd Nyatify
```

**2️⃣ 安装依赖：**

用 Python：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

用 Docker：

```bash
docker build -t nyatify .
docker run --rm -e DISCORD_TOKEN=your_bot_token_here nyatify
```

**3️⃣ 配置 Discord：**

[🎮 在 Discord 开发者门户创建一个机器人。](https://discord.com/developers/applications)

**a.** 到 Bot 页面，启用 Server Members Intent 和 Message Content Intent。

**b.** 在 OAuth2 或 Guild Install 里勾选 `bot` 和 `applications.commands`。

**c.** 机器人权限：Send Messages、Embed Links、Attach Files、Mention Everyone。

**d.** 用开发者门户生成的链接，把机器人邀请进服务器。

**4️⃣ 启动机器人：**

```bash
python nyatify.py
```

**🧪 测试：**

```bash
pip install -r requirements-dev.txt
pytest
```

## ⚙️ 配置

Nyatify 读的是 `config.txt`。把 `config.example.txt` 复制成 `config.txt`，再填上 `DISCORD_TOKEN`。

### 🔐 Discord

```bash
# 引号里只是示例。
# 写进 config.txt 时不要带引号。
# Token 从 Discord 开发者门户复制。
DISCORD_TOKEN="YOUR_BOT_TOKEN_HERE"

COMMAND_PREFIX="!"
LOG_LEVEL="INFO"
ENABLED_COGS="voice"  # 留空就加载全部 cog。

# 服务器 ID。留空会同步所有服务器，最多可能要一个小时。
SYNC_GUILD_ID="123456789012345678"

# 留空就不会显示 /admin。机器人重启后，所有者命令也会重新隐藏。
ADMIN_PASSWORD="YOUR_PASSWORD"
```

### 🏆 比赛

```bash
NEWS_POLL_MATCH="1"
THESPORTSDB_KEY="YOUR_THESPORTSDB_KEY"    # 留空会用免费的测试密钥。
PANDASCORE_TOKEN="YOUR_PANDASCORE_TOKEN"  # 留空就跳过 PandaScore。
```

### 🎫 哔哩哔哩和微博

```bash
NEWS_POLL_POST="1"

# bilibili.com 的 Cookie。留空就保持访客身份，不会登录。
BILI_COOKIE="SESSDATA=...; bili_jct=...; DedeUserID=123"

# bilibili.com 页面 localStorage 里的 ac_time_value。
BILI_AC_TIME="YOUR_AC_TIME_VALUE"

BILI_COOKIE_UID="0"                 # 0 表示还没存过哔哩哔哩登录。
WEIBO_COOKIES="SUB=YOUR_WEIBO_SUB"  # 留空的话，下次轮询时机器人会生成一个访客 SUB。
```

### 🌐 代理

```bash
BILI_PROXY="http://127.0.0.1:7890,http://127.0.0.1:7891"  # 留空就直连。

# 留空就直连。访客会固定使用创建它时的那个代理。
WEIBO_PROXY="http://127.0.0.1:7890"

AI_PROXY="http://127.0.0.1:7890"  # 留空就直连。
BILI_PHONES_PER_PROXY="1"
BILI_ACCOUNTS_PER_PHONE="2"
IP_CHECK="1"  # 填 0 就跳过出口 IP 检查。
```

### 🤖 AI

```bash
AI_API_KEY="YOUR_AI_API_KEY"  # 密钥、地址或模型有一个空着，就不会用 AI。

# 要填完整的对话接口地址，不要只填网站根地址。
AI_BASE_URL="https://api.deepseek.com/chat/completions"

AI_MODEL="deepseek-flash"
AI_ON_POLL="0"  # 填 1 的话，后台轮询也会跑模型。
```

## 🧩 Cogs

[语音](docs/voice.CN.md)和[动态](docs/news.CN.md)各自有一页，讲它们怎么工作、有哪些命令。

| Cog | 做什么 |
| --- | --- |
| 🔐 Admin | 所有者可以用它管理 cog，也可以给每个服务器设置通知频道。 |
| 🔊 Voice | 有人开始使用语音频道或舞台频道时发出的通知。 |
| 📰 News | 比赛卡片，还有哔哩哔哩和微博的动态。 |

下面是大家都能用的命令，还有所有者命令。

| 命令 | 说明 |
| --- | --- |
| `!ping` | 看看机器人在不在线。 |
| `/announce show` | 查看当前服务器的通知频道。 |
| `/announce feature` | 设置当前服务器的通知频道。 |
| `/admin` | 显示或隐藏所有者命令。 |
| `/cogs` | 列出已经加载的和可用的 cog。仅限所有者。 |
| `/load` | 加载一个 cog，不用重启。仅限所有者。 |
| `/unload` | 卸载一个 cog，不用重启。仅限所有者。 |
| `/reload` | 重新加载一个 cog，不用重启。仅限所有者。 |

## ⚠️ 限制

🔐 **Admin**

- 🙈 **所有者命令**：还没用过 `/admin` 时，这些命令不会出现。机器人重启后会再次隐藏。
- 🔒 **卸载**：admin 这个 cog 不能卸载。
- 💬 **私信**：这些命令只能在服务器里用，不能在私信里用。

🔊 **语音**

- 🚪 **空频道**：通知只在有人加入空着的语音频道或舞台频道时发出。
- 🤖 **机器人和 AFK**：机器人加入频道不会发出通知，挂机频道里也不会发。
- ⏱️ **一条通知**：这条消息会在 15 分钟后删掉。加入的人离开频道，或者进入挂机频道时，消息也会删掉。

📰 **动态**

- 🎫 **内容**：<u>只支持哔哩哔哩和微博。</u>
- 🔇 **话题**：话题默认不能改；筛选规则和其他设置可以自己改。
- 🕐 **开赛**：比赛卡片最早在开赛前 6 小时出现。
- 📅 **没比赛的日子**：开赛前 6 小时里如果没有比赛，大约一天才查一次。
- 🗑️ **完赛**：完赛过了一天半，比赛卡片就会删掉。

## 🙏 特别感谢

做这个机器人的时候，下面这些人和项目帮了我很多。

[DIYgod](https://github.com/DIYgod), [dataabc](https://github.com/dataabc), [SocialSisterYi](https://github.com/SocialSisterYi), [Nemo2011](https://github.com/Nemo2011)

- [RSSHub](https://github.com/DIYgod/RSSHub)：给很多网站做了订阅源，也包括哔哩哔哩和微博。
- [weibo-crawler](https://github.com/dataabc/weibo-crawler)：用 Python 写的微博爬虫。

🙇‍♂️🙇‍♂️🙇‍♂️ 非常感谢 [Cursor](https://cursor.com) 🙇‍♂️🙇‍♂️🙇‍♂️

## 📝 声明

Nyatify 是我用来**学习和练手**的个人项目。请遵守它访问的那些网站的**使用条款**。

请不要给哔哩哔哩、微博和比赛数据接口造成过大的**负担**。

不要把语音通知和动态发给**没有要求接收**的人。
