# 📰 News

English | [简体中文](news.CN.md)

Match cards for a sports club or an esports team, and posts from a Bilibili or Weibo account.

Cards go to this server's news announce channel. Set that channel with `/announce feature`.

# 🖼️ Example

<img src="../image/docs_image/news-game.png" alt="Game post: a Bilibili card with the account, the notice, and one image" width="560" />

# ⌨️ Commands

| Command | Description |
| --- | --- |
| `/news add` | Follow a platform account. |
| `/news feature` | Set the topic for a followed account. |
| `/news delete` | Stop following an account. |
| `/news show` | Show followed accounts. |
| `/news check` | Preview a recent post or the next game. Owner only. |
| `/news cookie` | Write a login cookie, or show status when empty. Owner only. |

# 🔑 Key points

**Match**: A sports club on **TheSportsDB**, or an esports team on **PandaScore**.

**Game, ticket, and other**: A **Bilibili** or **Weibo** account. A follow stays quiet until it has one of these topics.

**Kickoff**: A match card appears from 6 hours before kickoff, and not before that.

**Quiet days**: With no match in that window, the match poll checks about once a day.

**Full time**: The match card is removed a day and a half after full time.

**Sources**: <u>**Bilibili** and **Weibo** only.</u>

**Cookies**: Login cookies go in `config.txt`. The route is in Access.

**Owner commands**: `/news check` and `/news cookie` stay hidden until `/admin`.

# 🔐 Access

In `config.txt`, write the value without quotes.

**PandaScore**

> 1. Sign up at [app.pandascore.co/signup](https://app.pandascore.co/signup).
> 2. Open the [dashboard](https://app.pandascore.co/dashboard/main) and copy the token.
> 3. Set `PANDASCORE_TOKEN`. Empty skips PandaScore.

**TheSportsDB**

> No login. Empty `THESPORTSDB_KEY` uses the free test key.

**Bilibili**

> 1. Log in at [bilibili.com](https://www.bilibili.com).
> 2. Copy the Cookie header. It needs `SESSDATA`, `bili_jct`, and `DedeUserID`.
> 3. Copy `ac_time_value` from localStorage on that site.
> 4. Set `BILI_COOKIE` and `BILI_AC_TIME`.
> 5. [How to copy the cookie](https://github.com/ParrotXray/lavabili-plugin#manual-method-browser-devtools).

**Weibo**

> 1. Log in at [m.weibo.cn](https://m.weibo.cn).
> 2. Copy the `SUB` cookie.
> 3. Set `WEIBO_COOKIES`. Empty mints a guest `SUB` on the next poll.

**Guide**: [How to copy the cookie](https://github.com/dataabc/weibo-crawler#如何获取cookie可选).

# 🔁 Proxies

Bilibili and Weibo each take a proxy list. An empty list connects directly. Match cards stay direct.

**Add**: In `config.txt`, write the URLs without quotes. Separate more than one with a comma.

```
BILI_PROXY is Bilibili. WEIBO_PROXY is Weibo. For example, http://127.0.0.1:7890,http://127.0.0.1:7891.
```

**One proxy**: Every follow on that platform uses it. A Bilibili phone and a Weibo guest stay on the proxy that created them.

**Several**: The follow list is split across the proxies, in the order written.

```
Bilibili starts one proxy later each poll. BILI_PHONES_PER_PROXY phones share a proxy, and each phone asks BILI_ACCOUNTS_PER_PHONE accounts. The defaults are 1 and 2.

Weibo keeps the same accounts on the same proxy. A guest is not sent on another proxy.
```

**Left out**: A proxy that fails the exit check, or shares an exit IP, sits out that check. Its accounts go to the proxies that remain. Set `IP_CHECK` to `0` to skip the check.

**Login**: A saved login uses only the first proxy.

# 🎨 Customization

You are allowed to make changes manually in `news/cards.py` and `news/source/match/common.py`.

Customize the prompt to tell the model which posts to keep and what to put on the card.

```python
# news/prompt/post/filter.py
# FILTER_PROMPT: game, ticket. Which posts to keep.

# news/prompt/post/write.py
# WRITE_PROMPT: game, ticket, other. The card text.

# news/prompt/match/write.py
# MATCH_WRITE_PROMPT: the recap. Match has no filter.

# news/cards.py
_BILIBILI_COLOR = "#3DDCFF"
_WEIBO_COLOR = "#C25454"
_MATCH_COLOR = "#228B22"

# news/source/match/common.py
PREVIEW_BEFORE = timedelta(hours=6)
IDLE_REFRESH = timedelta(hours=24)
DELETE_AFTER_END = timedelta(days=1, hours=12)
```
