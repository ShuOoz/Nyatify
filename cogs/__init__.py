"""Feature modules.

Each `.py` file in this package is loaded as a discord.py extension.
To add a notify function: subclass announce.NotifyCog, set `feature`, and post
with send_announcement. Command-only functions can subclass Cog as usual.
To remove a function: delete or rename the file (prefix with `_` to skip it),
or unload it at runtime with the admin commands.
"""
