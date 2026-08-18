"""Pure MPRIS metadata parsing and transition stabilization."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from ..lyrics.match import TrackMetadata, clean_title, recover_artist

_MAX_TRACK_LENGTH_S = 24 * 60 * 60
_LYRICS_LOOKUP_MAX_LENGTH_S = 2 * 60 * 60

_NON_SONG_TITLE_MARKERS = (
    "complete performance",
    "official playlist",
    "online concert",
    "study with",
    "pet therapy",
    "music for pets",
    "一小時",
    "一小时",
    "合輯",
    "合集",
    "串燒",
    "精选",
    "精選",
    "演唱會",
    "オンラインコンサート",
    "完整演出",
    "單曲循環",
    "单曲循环",
)

# Chrome's own MPRIS bridge prefixes the tab's unread-notification count and
# appends the site name to the page title, e.g. "(3) Song - YouTube". Both are
# player noise, not part of the song: the count churns the identity key (forcing
# needless re-resolution) and the suffix wrecks title matching. Strip them so a
# browser-sourced title lines up with the clean one Plasma Browser Integration
# reports for the same track.
_TITLE_BADGE_PREFIX = re.compile(r"^\(\d+\)\s+")
_TITLE_SITE_SUFFIX = re.compile(r"\s*[-|–—]\s*YouTube(?:\s+Music)?\s*$", re.IGNORECASE)


def _clean_title(title: str, artist: str = "") -> str:
    cleaned = _TITLE_BADGE_PREFIX.sub("", title)
    cleaned = _TITLE_SITE_SUFFIX.sub("", cleaned)
    if artist and artist.casefold() in cleaned.casefold():
        artist_start = cleaned.casefold().find(artist.casefold())
        if artist_start > 0:
            before = cleaned[:artist_start].rstrip()
            remainder = cleaned[artist_start + len(artist) :]
            if remainder.lstrip().startswith(("-", "–", "—", "－")):
                trailing = remainder.lstrip(" \t\r\n-–—－")
                cleaned = artist if before.endswith(("-", "–", "—", "－")) and trailing else trailing
    cleaned = clean_title(cleaned, artist)
    cleaned = re.sub(r"『[^』]*動態歌詞[^』]*』", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"『[^』]*歌词[^』]*』", "", cleaned, flags=re.IGNORECASE)
    # Never strip a title down to nothing (a page literally titled "YouTube").
    return cleaned.strip() or title.strip()


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return " / ".join(str(item) for item in value if isinstance(item, str))
    return ""


def _length_seconds(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    length_us = float(value)
    if not math.isfinite(length_us):
        return None
    length_s = length_us / 1_000_000.0
    if length_s <= 0.0 or length_s > _MAX_TRACK_LENGTH_S:
        return None
    return length_s


def lyrics_lookup_reason(track: TrackInfo) -> str | None:
    """Return why lyrics should be skipped, or ``None`` when lookup is worthwhile."""
    if track.length_s is not None and track.length_s > _LYRICS_LOOKUP_MAX_LENGTH_S:
        return f"duration {track.length_s:.0f}s is longer than a normal song"

    title = (track.reported_title or track.title).casefold()
    for marker in _NON_SONG_TITLE_MARKERS:
        if marker.casefold() in title:
            return f"title contains non-song marker {marker!r}"

    # Counted on the same text the marker was found in. Counting words on the
    # cleaned title instead let "春天里 | 晴天 | 走马 Remix" through: cleaning keeps
    # only the first song, so the evidence of a medley was gone by then.
    words = (track.reported_title or track.title).split()
    if "remix" in title and len(words) >= 4 and sum(any("一" <= char <= "鿿" for char in word) for word in words) >= 3:
        return "title combines several song names"

    return None


@dataclass(frozen=True)
class TrackInfo:
    title: str
    artist: str
    album: str
    length_s: float | None
    track_id: str
    # What the player actually reported, before the upload grammar was stripped.
    # The non-song gate reads this: "is this a song at all" is a question about
    # the upload, and the markers that answer it (一小時, 串燒, KTV必唱) are the
    # very text the title cleaner removes. Defaults to the cleaned title so a
    # hand-built TrackInfo keeps the old behaviour.
    reported_title: str = ""
    url: str = ""

    def metadata(self) -> TrackMetadata:
        return TrackMetadata(self.title, self.artist, self.album, self.length_s)

    @property
    def identity_key(self) -> tuple[str, str, str, str]:
        return self.track_id, self.title, self.artist, self.album


def parse_metadata(raw: dict[str, Any]) -> TrackInfo:
    length_s = _length_seconds(raw.get("mpris:length"))
    reported = _as_text(raw.get("xesam:title"))
    artist = recover_artist(reported, _as_text(raw.get("xesam:artist")))
    return TrackInfo(
        title=_clean_title(reported, artist),
        artist=artist,
        album=_as_text(raw.get("xesam:album")),
        length_s=length_s,
        track_id=str(raw.get("mpris:trackid") or ""),
        reported_title=reported,
        url=_as_text(raw.get("xesam:url")),
    )


def unwrap(metadata: object) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        return {}
    return {
        key: getattr(variant, "value", variant)
        for key, variant in metadata.items()
        if isinstance(key, str)
    }


@dataclass(frozen=True)
class TrackObservation:
    player_name: str
    info: TrackInfo
    playback_status: str
    position_s: float | None
    observed_at: float
    player_identity: str = ""


@dataclass(frozen=True)
class TrackCommit:
    generation: int
    player_name: str
    info: TrackInfo
    # Player position (seconds) at the moment this track started, captured only on
    # a genuine A->B transition. For a player that reports a cumulative
    # playlist/video timeline instead of a song-relative one, subtracting this
    # realigns the position with the (0-based) lyric timestamps. ~0 for normal
    # players, so it is a no-op there. None on the first track (join point unknown).
    start_position: float | None = None
    player_identity: str = ""


class TrackStabilizer:
    def __init__(self) -> None:
        self._candidate_key: tuple[object, ...] | None = None
        self._candidate: TrackObservation | None = None
        self._candidate_start: float | None = None
        self._changed_at = 0.0
        self._committed_key: tuple[object, ...] | None = None
        self._generation = 0
        self._transitioning = False

    def observe(self, observation: TrackObservation) -> TrackCommit | None:
        info = observation.info
        if not info.title and not info.artist:
            self._transitioning = self._committed_key is not None
            self._candidate_key = None
            self._candidate = None
            return None

        key = (observation.player_name, *info.identity_key)
        if key != self._candidate_key:
            self._candidate_key = key
            self._candidate = observation
            self._candidate_start = observation.position_s  # position when this track first appeared
            self._changed_at = observation.observed_at
            self._transitioning = key != self._committed_key
            return None

        settle_seconds = 0.35 if info.artist else 0.8
        if self._committed_key is not None:
            previous_title = self._committed_key[2]
            previous_artist = self._committed_key[3]
            if info.title != previous_title and info.artist and info.artist == previous_artist:
                settle_seconds = max(settle_seconds, 0.8)
        if observation.observed_at - self._changed_at < settle_seconds:
            return None
        if key == self._committed_key:
            self._transitioning = False
            return None

        # Only a genuine A->B change yields a start offset; the very first track has
        # no known join point, so leave it None (no correction).
        # At track boundary, stale Position may arrive with new Metadata. If used as
        # offset, it ruins all subsequent tracks. Detect reset by comparing settlement
        # pos vs first sighting: if lower → song-relative (start=0); else cumulative.
        if self._committed_key is None:
            # case 1: first track, no previous track to compare with
            start_position = None
        else:
            # case 2: subsequent track, compare positions to detect if the player reset
            settled = observation.position_s
            candidate = self._candidate_start

            if settled is not None and candidate is not None and settled < candidate - 0.5:
                start_position = 0.0
            else:
                start_position = candidate

        self._committed_key = key
        self._generation += 1
        self._transitioning = False
        return TrackCommit(self._generation, observation.player_name, info, start_position, observation.player_identity)

    @property
    def transitioning(self) -> bool:
        return self._transitioning

    def reset(self) -> None:
        self._candidate_key = None
        self._candidate = None
        self._candidate_start = None
        self._changed_at = 0.0
        self._committed_key = None
        self._transitioning = False
