"""The player card."""

__all__ = ["MusicPlayer"]


def __getattr__(name: str):
    # Playback imports the radio module, and the card imports playback.
    # The card stays out of this import so those two can load.
    if name == "MusicPlayer":
        from music.player.card import MusicPlayer

        return MusicPlayer
    raise AttributeError(f"module {__name__!r} has no attribute {name}")
