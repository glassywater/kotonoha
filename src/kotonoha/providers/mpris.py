"""Stable MPRIS sampling and ordered external-lyrics resolution."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import replace
from typing import Any, Protocol

import aiohttp

from ..config import DEFAULT_LYRICS_SOURCES
from ..lyrics.hint import LyricsHint, from_player
from ..lyrics.match import TrackMetadata
from ..lyrics.resolver import LyricsResolver, ResolvedLyrics
from ..lyrics.select import build_snapshot, find_current_index
from ..model import LyricLine
from ..players import PlayerInfo
from ..state import LyricsState
from .gate import SourceGate
from .mpris_track import (
    TrackCommit,
    TrackInfo,
    TrackObservation,
    TrackStabilizer,
    lyrics_lookup_reason,
    parse_metadata,
)
from .mpris_track import (
    unwrap as _unwrap,
)

logger = logging.getLogger(__name__)

MPRIS_PREFIX = "org.mpris.MediaPlayer2."
MPRIS_PATH = "/org/mpris/MediaPlayer2"
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
DBUS_NAME = "org.freedesktop.DBus"
DBUS_PATH = "/org/freedesktop/DBus"
# One second filters out same-poll observation jitter without delaying a deliberate new start.
RECENT_PLAYER_MARGIN = 1.0

MPRIS_INTROSPECTION = """<!DOCTYPE node PUBLIC "-//freedesktop//DTD D-BUS Object Introspection 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/introspect.dtd">
<node>
  <interface name="org.freedesktop.DBus.Properties">
    <method name="Get">
      <arg name="interface_name" type="s" direction="in"/>
      <arg name="property_name" type="s" direction="in"/>
      <arg name="value" type="v" direction="out"/>
    </method>
    <method name="GetAll">
      <arg name="interface_name" type="s" direction="in"/>
      <arg name="props" type="a{sv}" direction="out"/>
    </method>
    <signal name="PropertiesChanged">
      <arg name="interface_name" type="s"/>
      <arg name="changed_properties" type="a{sv}"/>
      <arg name="invalidated_properties" type="as"/>
    </signal>
  </interface>
  <interface name="org.mpris.MediaPlayer2.Player">
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="Metadata" type="a{sv}" access="read"/>
    <property name="Position" type="x" access="read"/>
    <property name="Rate" type="d" access="read"/>
    <signal name="Seeked">
      <arg name="Position" type="x"/>
    </signal>
  </interface>
  <interface name="org.mpris.MediaPlayer2">
    <property name="Identity" type="s" access="read"/>
  </interface>
</node>"""


class ResolverLike(Protocol):
    async def resolve(
        self,
        session: Any,
        track: TrackMetadata,
        sources: list[str],
        /,
    ) -> ResolvedLyrics | None: ...

    def reset_memory(self) -> None: ...

    def set_cache_enabled(self, enabled: bool, /) -> None: ...

    def set_prefer_best(self, enabled: bool, /) -> None: ...

    def set_fuzzy(self, enabled: bool, /) -> None: ...

    async def clear_cache(self) -> None: ...

    async def resolve_hint(
        self, session: Any, track: TrackMetadata, sources: list[str], hint: LyricsHint, /
    ) -> ResolvedLyrics | None: ...


async def _connect() -> Any:
    from dbus_fast.aio import MessageBus
    from dbus_fast.constants import BusType

    return await MessageBus(bus_type=BusType.SESSION).connect()


async def list_players(bus: Any) -> list[str]:
    introspection = await bus.introspect(DBUS_NAME, DBUS_PATH)
    obj = bus.get_proxy_object(DBUS_NAME, DBUS_PATH, introspection)
    iface = obj.get_interface(DBUS_NAME)
    names = await iface.call_list_names()
    return sorted(name for name in names if name.startswith(MPRIS_PREFIX))


async def _player_interface(bus: Any, name: str) -> Any:
    obj = bus.get_proxy_object(name, MPRIS_PATH, MPRIS_INTROSPECTION)
    return obj.get_interface(PLAYER_IFACE)


class MprisProvider:
    def __init__(
        self,
        state: LyricsState,
        poll_interval: float = 0.2,
        *,
        lyrics_sources: list[str] | None = None,
        gate: SourceGate | None = None,
        resolver: ResolverLike | None = None,
    ) -> None:
        self._state = state
        self._poll_interval = poll_interval
        self._lyrics_sources = lyrics_sources if lyrics_sources is not None else list(DEFAULT_LYRICS_SOURCES)
        self._player_lock = ""
        self._gate = gate or SourceGate()
        self._resolver: ResolverLike = resolver or LyricsResolver(gate=self._gate)
        self._bus: Any = None
        self._session: aiohttp.ClientSession | None = None
        self._task: asyncio.Task[None] | None = None
        self._poll_wakeup = asyncio.Event()
        self._stabilizer = TrackStabilizer()
        self._empty_since: float | None = None
        self._song_offset = 0.0  # subtract from a cumulative playlist/video position
        self._lines: list[LyricLine] = []
        self._last_index = -2
        self._current_name: str | None = None
        self._playing_since: dict[str, float] = {}
        self._props_iface: Any = None
        self._subscribed_name: str | None = None
        self._load_task: asyncio.Task[None] | None = None
        self._load_tasks: set[asyncio.Task[None]] = set()
        self._current_commit: TrackCommit | None = None
        self._content_owner = "none"
        self._provider_name = ""
        self._cache_enabled = True
        self._prefer_best = True
        self._fuzzy = True
        self._gate_revision = self._gate.revision
        self._calibration_generation: int | None = None
        self._calibration_until: float = 0.0
        self._calibration_offset: float | None = None

    def set_lyrics_sources(self, sources: list[str]) -> None:
        updated = list(sources)
        if updated == self._lyrics_sources:
            return
        self._lyrics_sources = updated
        self._resolver.reset_memory()
        self._force_reload()

    def set_player_lock(self, bus_name: str) -> None:
        updated = bus_name if isinstance(bus_name, str) else ""
        if updated == self._player_lock:
            return
        self._player_lock = updated
        self._poll_wakeup.set()

    async def available_players(self) -> list[PlayerInfo]:
        if self._bus is None:
            return []
        result: list[PlayerInfo] = []
        records: list[tuple[Any, str, str, TrackInfo]] = []
        for name in await list_players(self._bus):
            try:
                obj = self._bus.get_proxy_object(name, MPRIS_PATH, MPRIS_INTROSPECTION)
                props = obj.get_interface("org.freedesktop.DBus.Properties")
                identity = await props.call_get("org.mpris.MediaPlayer2", "Identity")
            except Exception as exc:  # noqa: BLE001 - player may vanish during discovery
                logger.debug("player identity read failed for %s: %s", name, exc)
                continue
            # A single property arrives as a Variant, not the a{sv} map _unwrap
            # takes: passing it there yields "{}" as every player's display name.
            identity_text = str(getattr(identity, "value", identity) or "")
            # The status and metadata read is separate: it is what the row shows
            # beside the name, not what makes the player exist. Sharing one try with
            # the identity read dropped a reachable player from the picker entirely
            # whenever this optional detail failed.
            status, info = "", TrackInfo("", "", "", None, "")
            try:
                values = _unwrap(await props.call_get_all(PLAYER_IFACE))
                status = str(values.get("PlaybackStatus") or "")
                info = parse_metadata(_unwrap(values.get("Metadata", {})))
            except Exception as exc:  # noqa: BLE001 - detail is optional for the row
                logger.debug("player detail read failed for %s: %s", name, exc)
            records.append((None, name, status, info))
            result.append(PlayerInfo(name, identity_text, info.title, info.artist, status))
        automatic_name = self._automatic_name(records)
        return [
            PlayerInfo(p.bus_name, p.identity, p.title, p.artist, p.playback_status, p.bus_name == automatic_name)
            for p in result
        ]

    def set_cache_enabled(self, enabled: bool) -> None:
        updated = bool(enabled)
        if updated == self._cache_enabled:
            return
        self._cache_enabled = updated
        self._resolver.set_cache_enabled(updated)
        self._force_reload()

    def set_prefer_best(self, enabled: bool) -> None:
        updated = bool(enabled)
        if updated == self._prefer_best:
            return
        self._prefer_best = updated
        self._resolver.set_prefer_best(updated)
        self._force_reload()

    def set_fuzzy(self, enabled: bool) -> None:
        updated = bool(enabled)
        if updated == self._fuzzy:
            return
        self._fuzzy = updated
        self._resolver.set_fuzzy(updated)
        self._force_reload()

    # --- player selection (ported from waylyrics: live pick the active MPRIS player) ---

    def current_player(self) -> str | None:
        """The bus name currently preferred/selected (None = auto)."""
        return self._current_name

    def select_player(self, name: str | None) -> None:
        """Pin playback to a specific MPRIS player (bus name) or None for auto.

        The selection survives while that player is present/playing; if it
        disappears, ``_active_player`` falls back to automatic choice.
        """
        self._current_name = name or None
        self._force_reload()

    async def list_available_players(self) -> list[tuple[str, str]]:
        """List (bus_name, Identity) for every reachable MPRIS player (sorted)."""
        if self._bus is None:
            return []
        out: list[tuple[str, str]] = []
        for name in await list_players(self._bus):
            identity = await self._safe_identity(name)
            out.append((name, identity or name))
        return out

    async def _safe_identity(self, name: str) -> str | None:
        try:
            obj = self._bus.get_proxy_object(name, MPRIS_PATH, MPRIS_INTROSPECTION)
            props = obj.get_interface("org.freedesktop.DBus.Properties")
            value = await props.call_get("org.mpris.MediaPlayer2", "Identity")
        except Exception as exc:  # noqa: BLE001 - D-Bus boundary
            logger.debug("identity read failed for %s: %s", name, exc)
            return None
        # call_get returns a Variant; unwrap it.
        if hasattr(value, "value"):
            value = value.value
        return value if isinstance(value, str) else None

    async def clear_cache(self) -> None:
        await self._resolver.clear_cache()

    async def start(self) -> None:
        self._bus = await _connect()
        # Generous session-wide safety net only. Each provider sets its own tighter
        # per-request timeout (netease is fast, lrclib is routinely slow), because a
        # single short shared budget killed every lrclib fetch — its backend often
        # takes 7-9s to answer — leaving that whole fallback source silently dead.
        timeout = aiohttp.ClientTimeout(total=20.0, connect=5.0)
        self._session = aiohttp.ClientSession(timeout=timeout)
        self._task = asyncio.create_task(self._run())
        logger.info("MPRIS provider started")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

        tasks = tuple(self._load_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._load_task = None

        if self._session is not None:
            await self._session.close()
            self._session = None
        if self._props_iface is not None:
            with contextlib.suppress(Exception):
                self._props_iface.off_properties_changed(self._on_props_changed)
            self._props_iface = None
            self._subscribed_name = None
        if self._bus is not None:
            self._bus.disconnect()
            self._bus = None

    async def _run(self) -> None:
        try:
            while True:
                self._poll_wakeup.clear()
                try:
                    await self._poll_once()
                except Exception as exc:  # noqa: BLE001 - D-Bus boundary; keep polling
                    logger.debug("MPRIS poll error: %s", exc)
                try:
                    await asyncio.wait_for(self._poll_wakeup.wait(), timeout=self._poll_interval)
                except TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass

    async def _safe_iface(self, name: str) -> Any:
        try:
            return await _player_interface(self._bus, name)
        except Exception as exc:  # noqa: BLE001 - player may have vanished
            logger.debug("interface %s failed: %s", name, exc)
            return None

    @staticmethod
    async def _safe_status(player: Any) -> str:
        try:
            return await player.get_playback_status()
        except Exception as exc:  # noqa: BLE001 - D-Bus boundary
            logger.debug("status read failed: %s", exc)
            return ""

    @staticmethod
    async def _safe_info(player: Any) -> TrackInfo | None:
        try:
            return parse_metadata(_unwrap(await player.get_metadata()))
        except Exception as exc:  # noqa: BLE001 - D-Bus boundary
            logger.debug("metadata read failed while selecting player: %s", exc)
            return None

    def _selection_score(self, record: tuple[Any, str, str, TrackInfo]) -> tuple[int, int, int, int]:
        """Rank a Playing candidate by recency, metadata completeness, then continuity."""
        _player, name, _status, info = record
        current_started = self._playing_since.get(self._current_name or "")
        candidate_started = self._playing_since.get(name)
        started_more_recently = int(
            current_started is not None
            and candidate_started is not None
            and candidate_started - current_started > RECENT_PLAYER_MARGIN
        )
        return (
            started_more_recently,
            1 if info.artist else 0,  # a real artist beats a title-only source
            1 if info.title else 0,
            1 if name == self._current_name else 0,  # break ties toward the current source
        )

    def _automatic_name(self, records: list[tuple[Any, str, str, TrackInfo]]) -> str | None:
        chosen = self._choose_record(records)
        return chosen[1] if chosen is not None else None

    def _choose_record(
        self, records: list[tuple[Any, str, str, TrackInfo]]
    ) -> tuple[Any, str, str, TrackInfo] | None:
        """Pick the player to follow, or None when nothing qualifies.

        One policy for both the poll and the picker. The picker used to carry its
        own copy that ordered the last two fallbacks the other way round, so with a
        Playing player reporting no metadata beside a Paused one that reports a
        track, the settings row marked a player as Current that the poll would not
        have followed.
        """
        eligible = [record for record in records if record[2] in {"Playing", "Paused"}]
        locked = next((record for record in eligible if record[1] == self._player_lock), None)
        if locked is not None:
            return locked
        playing_with_track = [
            record for record in eligible if record[2] == "Playing" and (record[3].title or record[3].artist)
        ]
        if playing_with_track:
            # Two players can expose the *same* track: Chrome's own MPRIS and the
            # Plasma Browser Integration bridge both appear for YouTube Music.
            # Chrome sorts first alphabetically and reports a title polluted with
            # " - YouTube" plus an empty artist, so taking the first Playing source
            # picked it every time and matching silently failed. Take the most
            # complete metadata instead; ties keep the current/first source.
            return max(playing_with_track, key=self._selection_score)
        current = next((record for record in eligible if record[1] == self._current_name), None)
        if current is not None and current[2] == "Paused" and (current[3].title or current[3].artist):
            return current
        paused = next(
            (record for record in eligible if record[2] == "Paused" and (record[3].title or record[3].artist)),
            None,
        )
        if paused is not None:
            return paused
        return next((record for record in eligible if record[2] == "Playing"), None)

    async def _active_player(self, *, now: float | None = None) -> tuple[Any, str] | None:
        observed_at = time.monotonic() if now is None else now
        names = await list_players(self._bus)
        present = set(names)
        for name in tuple(self._playing_since):
            if name not in present:
                del self._playing_since[name]
        ordered = list(names)
        if self._current_name in ordered:
            ordered.remove(self._current_name)
            ordered.insert(0, self._current_name)

        collected: list[tuple[Any, str, str, TrackInfo]] = []
        for name in ordered:
            player = await self._safe_iface(name)
            if player is None:
                self._playing_since.pop(name, None)
                continue
            status = await self._safe_status(player)
            if status == "Playing":
                self._playing_since.setdefault(name, observed_at)
            else:
                self._playing_since.pop(name, None)
            if status not in {"Playing", "Paused"}:
                continue
            info = await self._safe_info(player)
            if info is None and name != self._player_lock:
                continue
            if info is None:
                info = TrackInfo("", "", "", None, "")
            # Collected in poll order, with the current player already moved to the
            # front, so the shared policy sees the same ordering the picker gives it.
            collected.append((player, name, status, info))

        selected = self._choose_record(collected)
        if selected is None:
            self._current_name = None
            return None
        self._current_name = selected[1]
        return selected[0], selected[1]

    async def _ensure_subscribed(self, name: str) -> None:
        if name == self._subscribed_name and self._props_iface is not None:
            return
        if self._props_iface is not None:
            with contextlib.suppress(Exception):
                self._props_iface.off_properties_changed(self._on_props_changed)
            self._props_iface = None
        try:
            obj = self._bus.get_proxy_object(name, MPRIS_PATH, MPRIS_INTROSPECTION)
            props = obj.get_interface("org.freedesktop.DBus.Properties")
            props.on_properties_changed(self._on_props_changed)
            self._props_iface = props
            self._subscribed_name = name
        except Exception as exc:  # noqa: BLE001 - signals are best effort
            logger.debug("subscribe failed for %s: %s", name, exc)
            self._props_iface = None
            self._subscribed_name = None

    def _on_props_changed(self, interface: str, changed: dict[str, Any], invalidated: list[str]) -> None:
        if interface != PLAYER_IFACE:
            return
        interesting = {"Metadata", "PlaybackStatus"}
        if interesting.intersection(changed) or interesting.intersection(invalidated):
            self._poll_wakeup.set()

    async def _poll_once(self, *, now: float | None = None) -> None:
        observed_at = time.monotonic() if now is None else now
        active = await self._active_player(now=observed_at)
        if active is None:
            self._handle_no_player(observed_at)
            return

        player, name = active
        await self._ensure_subscribed(name)
        status = await self._safe_status(player)
        if status not in {"Playing", "Paused"}:
            self._handle_no_player(observed_at)
            return

        try:
            identity = await self._safe_identity(name)
            first_info = parse_metadata(_unwrap(await player.get_metadata()))
        except Exception as exc:  # noqa: BLE001 - D-Bus boundary
            logger.debug("metadata sample failed: %s", exc)
            return

        position: float | None = None
        try:
            raw_position = await player.get_position()
            if isinstance(raw_position, (int, float)) and not isinstance(raw_position, bool):
                position = float(raw_position) / 1_000_000.0
        except Exception as exc:  # noqa: BLE001 - Position is optional
            logger.debug("position read failed: %s", exc)

        try:
            second_info = parse_metadata(_unwrap(await player.get_metadata()))
        except Exception as exc:  # noqa: BLE001 - D-Bus boundary
            logger.debug("metadata verification failed: %s", exc)
            return
        if first_info.identity_key != second_info.identity_key:
            self._stabilizer.observe(
                TrackObservation(
                    player_name=name,
                    info=TrackInfo("", "", "", None, ""),
                    playback_status=status,
                    position_s=position,
                    observed_at=observed_at,
                )
            )
            self._poll_wakeup.set()
            return

        info = second_info
        observation = TrackObservation(name, info, status, position, observed_at, identity or "")
        commit = self._stabilizer.observe(observation)
        if not info.title and not info.artist:
            if status == "Playing":
                self._empty_since = None
            return
        self._empty_since = None

        if commit is not None:
            self._schedule_load(commit)

        if position is not None and not self._stabilizer.transitioning:
            current = self._current_commit
            if current is not None:
                self._calibrate_offset(current, position, observed_at)

        if not self._stabilizer.transitioning:
            self._ensure_content_owner()
        if self._stabilizer.transitioning or self._content_owner != "external":
            return

        current = self._current_commit
        if current is None:
            return
        playing = status == "Playing"
        if position is not None:
            position = max(0.0, position - self._song_offset)  # song-relative (no-op when offset ~0)
        cider_timing = self._gate.current_timing(current.info.metadata())
        if cider_timing is not None and cider_timing.current_time is not None:
            position = cider_timing.current_time  # already song-relative; ignore the offset
            if cider_timing.is_playing is not None:
                playing = cider_timing.is_playing
        if position is None:
            return
        self._state.tick(position, playing)
        index = find_current_index(self._lines, position)
        if index != self._last_index:
            self._last_index = index
            self._emit(current.info, position, playing)

    def _handle_no_player(self, now: float) -> None:
        if self._current_commit is None and self._content_owner == "none":
            return
        if self._empty_since is None:
            self._empty_since = now
            return
        if now - self._empty_since >= 0.35:
            self._reset()

    def _schedule_load(self, commit: TrackCommit) -> None:
        current = self._current_commit
        if current is not None and commit != current and commit.generation <= current.generation:
            commit = TrackCommit(
                current.generation + 1,
                commit.player_name,
                commit.info,
                commit.start_position,
                commit.player_identity,
            )
        if self._load_task is not None and not self._load_task.done():
            self._load_task.cancel()
        # A transition carries the song's start position; adopt it as the offset so
        # the sweep uses song-relative time. Reloads of the same song (start_position
        # None, e.g. a source/cache change) keep the current offset.
        if commit.start_position is not None:
            self._song_offset = commit.start_position
            # For song-relative players, the Position reset may arrive after the stabilizer's
            # settle window has closed. Open a generation-bound short window to continue
            # observing raw Position; if it drops below the offset threshold within this
            # window, it means the committed offset was captured from the previous song's
            # stale position — so correct the offset to 0.
            if commit.start_position > 0.0:
                self._calibration_generation = commit.generation
                self._calibration_until = time.monotonic() + 2.0
                self._calibration_offset = commit.start_position
            else:
                self._calibration_generation = None
                self._calibration_offset = None
        self._current_commit = commit
        self._content_owner = "resolving"
        task = asyncio.create_task(self._load_song(commit))
        self._load_task = task
        self._load_tasks.add(task)
        task.add_done_callback(self._load_finished)

    def _load_finished(self, task: asyncio.Task[None]) -> None:
        self._load_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.warning("MPRIS lyrics load failed: %s", error)

    async def _load_song(self, commit: TrackCommit) -> None:
        self._lines = []
        self._last_index = -2
        self._provider_name = ""
        self._gate.select_external()
        self._state.update(
            build_snapshot(
                [],
                0.0,
                provider="MPRIS",
                song_id=None,
                title=commit.info.title,
                artist=commit.info.artist,
                is_playing=True,
            )
        )
        # Resolve the duration before the gate, not after: a browser often reports
        # none at all while Cider knows the real length, and running the gate first
        # let a 2-hour stream through on a missing MPRIS length.
        info = commit.info
        cider_timing = self._gate.current_timing(info.metadata())
        if cider_timing is not None and cider_timing.duration_s is not None:
            if cider_timing.duration_s != info.length_s:
                logger.debug(
                    "Using matching Cider duration %.3fs instead of MPRIS %s",
                    cider_timing.duration_s,
                    info.length_s,
                )
            info = replace(info, length_s=cider_timing.duration_s)

        skip_reason = lyrics_lookup_reason(info)
        if skip_reason is not None:
            # A 14-hour compilation is not a song; querying every provider for it
            # costs traffic and can match a title that merely appears inside it.
            logger.info("Skipping lyric lookup for %r: %s", info.title, skip_reason)
            self._content_owner = "none"
            return
        track = info.metadata()
        try:
            hint = from_player(
                commit.player_identity, commit.player_name, commit.info.track_id, commit.info.url
            )
            result = (
                await self._resolver.resolve_hint(self._session, track, self._lyrics_sources, hint)
                if hint is not None
                else None
            )
            if result is None:
                result = await self._resolver.resolve(self._session, track, self._lyrics_sources)
        except asyncio.CancelledError:
            raise
        if self._current_commit != commit:
            return
        if result is None:
            self._content_owner = "none"
            self._select_late_cider()
            return
        if result.source == "cider" and result.live_snapshot is not None:
            self._content_owner = "cider"
            self._provider_name = "cider"
            self._gate_revision = self._gate.revision
            self._state.update(result.live_snapshot)
            return
        if self._select_late_cider(before_source=result.source):
            return
        self._content_owner = "external"
        self._provider_name = result.source
        self._gate_revision = self._gate.revision
        self._lines = list(result.lines)
        logger.info(
            "MPRIS %r / %r -> %d %s lines",
            commit.info.title,
            commit.info.artist,
            len(self._lines),
            result.source,
        )

    def _force_reload(self) -> None:
        current = self._current_commit
        if current is None:
            return
        self._schedule_load(
            TrackCommit(
                current.generation + 1,
                current.player_name,
                current.info,
                player_identity=current.player_identity,
            )
        )

    def _ensure_content_owner(self) -> None:
        if self._content_owner == "cider" and not self._gate.cider_active:
            self._force_reload()
            return
        if self._content_owner != "none" or self._current_commit is None:
            return
        self._select_late_cider()

    def _select_late_cider(self, *, before_source: str | None = None) -> bool:
        if self._current_commit is None:
            return False
        revision = self._gate.revision
        if revision == self._gate_revision:
            return False
        self._gate_revision = revision
        if "cider" not in self._lyrics_sources:
            return False
        if before_source is not None:
            try:
                if self._lyrics_sources.index("cider") >= self._lyrics_sources.index(before_source):
                    return False
            except ValueError:
                return False
        match = self._gate.current_match(self._current_commit.info.metadata())
        if match is None:
            return False
        self._gate.select_cider(match.client_id)
        self._content_owner = "cider"
        self._provider_name = "cider"
        self._state.update(match.snapshot)
        return True

    def _emit(self, info: TrackInfo, position: float, playing: bool) -> None:
        provider = f"MPRIS:{self._provider_name}" if self._provider_name else "MPRIS"
        self._state.update(
            build_snapshot(
                self._lines,
                position,
                provider=provider,
                song_id=None,
                title=info.title,
                artist=info.artist,
                is_playing=playing,
            )
        )

    def _calibrate_offset(self, commit: TrackCommit, raw_position: float, observed_at: float) -> None:
        if (
            self._calibration_generation == commit.generation
            and observed_at <= self._calibration_until
            and self._calibration_offset is not None
            and raw_position < self._calibration_offset - 0.5
        ):
            logger.debug(
                "MPRIS calibration: offset %.3fs -> 0.0 (raw %.3fs, gen %d)",
                self._calibration_offset,
                raw_position,
                commit.generation,
            )
            self._song_offset = 0.0
            self._last_index = -2
            self._calibration_generation = None
            self._calibration_offset = None

    def _reset(self) -> None:
        if self._load_task is not None and not self._load_task.done():
            self._load_task.cancel()
        self._stabilizer.reset()
        self._current_commit = None
        self._lines = []
        self._last_index = -2
        self._content_owner = "none"
        self._provider_name = ""
        self._empty_since = None
        self._song_offset = 0.0
        self._calibration_generation = None
        self._calibration_offset = None
        self._gate.select_standalone()
        self._gate_revision = self._gate.revision
        self._state.clear()


async def probe() -> None:
    bus = await _connect()
    players = await list_players(bus)
    if not players:
        print("No MPRIS players found. Start a player (browser YTM / Spotify / VLC) and retry.")
        return

    print(f"Found {len(players)} MPRIS player(s): {', '.join(players)}")
    for name in players:
        print(f"\n=== {name} ===")
        try:
            player = await _player_interface(bus, name)
            status = await player.get_playback_status()
            info = parse_metadata(_unwrap(await player.get_metadata()))
            print(f"  status   = {status}")
            print(f"  title    = {info.title!r}")
            print(f"  artist   = {info.artist!r}")
            print(f"  length   = {info.length_s}s")
            print("  Position once/sec - does delta advance about 1.0 while playing?")
            last: float | None = None
            for _ in range(6):
                try:
                    pos_s = (await player.get_position()) / 1_000_000.0
                except Exception as exc:  # noqa: BLE001 - diagnostic command
                    print(f"    Position read failed: {exc}")
                    break
                delta = "" if last is None else f"   delta = {pos_s - last:+.3f}"
                print(f"    position = {pos_s:8.3f}s{delta}")
                last = pos_s
                await asyncio.sleep(1.0)
        except Exception as exc:  # noqa: BLE001 - diagnostic command
            print(f"  error reading player: {exc}")


def main() -> None:
    asyncio.run(probe())


if __name__ == "__main__":
    main()
