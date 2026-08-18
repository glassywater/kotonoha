"""What the UI needs to know about a media player, without knowing the transport.

The settings window lists the players it can lock onto. Describing a row needs
none of D-Bus, so this type lives apart from the provider that discovers them
and apart from the lyrics model, which is about lyric payloads rather than
players.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PlayerInfo:
    """One media player, as a row in the picker."""

    bus_name: str
    identity: str
    # What the row shows besides the name, so two browsers can be told apart.
    title: str = ""
    artist: str = ""
    playback_status: str = ""
    automatic: bool = False


__all__ = ["PlayerInfo"]
