# 🔊 Voice

English | [简体中文](voice.CN.md)

A notice when a person starts an empty voice or stage channel.

The card goes to this server's voice announce channel.

# 🖼️ Example

<img src="../image/docs_image/voice-notice.png" alt="Voice notice: a ping, then a card for the person who joined" width="560" />

# ⌨️ Commands

| Command | Description |
| --- | --- |
| — | No special command. |

# 🔑 Key points

**Empty channel**: A notice starts only when a person joins an empty voice or stage channel.

**Bots and AFK**: Bots do not start a notice, and the AFK channel is ignored.

**One notice**: Staying in the channel does not send another notice.

**Return**: Coming back within 45 seconds keeps the same notice.

**Lifetime**: The message is removed after 15 minutes.

# 🎨 Customization

You are allowed to make changes manually in `cogs/voice_notify.py`.

Default icon / banner: `image/nyatify_icon.png` / `image/nyatify_banner.png`

```python
# cogs/voice_notify.py
VOICE_NOTICE_CONTENT = "@everyone !!! OH MY GAH !!!"
VOICE_NOTICE_TITLE = "Hello Every Nya ฅ^•ﻌ•^ฅ"
# VOICE_NOTICE_GIF_URLS: 400×180.
NOTICE_TTL_SECONDS = 15 * 60  # 15 minutes
RECONNECT_GRACE_SECONDS = 45.0  # 45 seconds

# announce.py
ACCENT_COLOR = "#FFB6C1"
```
