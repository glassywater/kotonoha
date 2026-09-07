import asyncio
import logging

import pytest

from kotonoha.app.display_coordinator import DisplayCoordinator
from kotonoha.app.source_gate import SourceOwnershipCoordinator
from kotonoha.display.models import DisplayState
from kotonoha.display.presentation import DisplayEngine
from kotonoha.display.timeline import TimelineEngine
from kotonoha.lyrics.artifact import LyricsArtifact
from kotonoha.lyrics.cache import (
    CacheDeleteResult,
    CacheDeleteStatus,
    CacheWriteResult,
    CacheWriteStatus,
    LyricsCacheEntry,
    LyricsCacheKey,
    LyricsCacheMode,
    LyricsCacheQuery,
)
from kotonoha.lyrics.models import LyricLine, LyricsDocument, TimingKind
from kotonoha.lyrics.sources import LyricsSourceResult
from kotonoha.lyrics.workflow import ResolverLookup, ResolverPort
from kotonoha.playback.models import MprisPlayerPort, PlaybackObservation, PlaybackStatus, TrackIdentity
from kotonoha.providers.mpris import MprisProvider
from kotonoha.providers.mpris_adapter import MprisPlaybackAdapter
from kotonoha.providers.mpris_lyrics import MprisLyricsCoordinator
from kotonoha.providers.mpris_playback import MprisPlaybackCoordinator, PlaybackSample
from kotonoha.providers.mpris_resolution import MprisResolutionSession
from kotonoha.providers.mpris_track import TrackCommit, TrackInfo, parse_metadata
from kotonoha.ui.overlay.publisher import QtDisplayPublisher
from kotonoha.ui.overlay.state import LyricsState


def _display(state: LyricsState) -> DisplayCoordinator:
    """Build the application display coordinator with its Qt test publisher."""
    return DisplayCoordinator(
        QtDisplayPublisher(state),
        presenter=DisplayEngine(),
        timeline=TimelineEngine(),
    )

VALID_METADATA: dict[str, object] = {
    "xesam:title": "Song",
    "xesam:artist": ["Artist"],
    "xesam:album": "Album",
    "mpris:length": 180_000_000,
    "mpris:trackid": "/track/1",
}


class FakePlayer:
    def __init__(self, metadata: dict[str, object], *, position: int = 0) -> None:
        self.metadata = metadata
        self.position = position
        self.status = "Playing"

    async def get_playback_status(self) -> str:
        return self.status

    async def get_metadata(self) -> dict[str, object]:
        return self.metadata

    async def get_position(self) -> int:
        return self.position


class FakeSession:
    def __init__(self, player: FakePlayer | None, *, fail_close: bool = False) -> None:
        self.player_value = player
        self.connected = True
        self.closed = False
        self.close_calls = 0
        self.fail_close = fail_close
        self.subscriptions: list[str] = []

    async def connect(self) -> None:
        self.connected = True
        self.closed = False

    def close(self) -> None:
        self.close_calls += 1
        self.closed = True
        self.connected = False
        if self.fail_close:
            raise RuntimeError("session close failed")

    async def player_names(self) -> list[str]:
        return ["org.mpris.MediaPlayer2.test"] if self.player_value is not None else []

    async def player(self, _name: str) -> MprisPlayerPort | None:
        return self.player_value

    async def status(self, player: MprisPlayerPort) -> str:
        return await player.get_playback_status()

    async def track(self, player: MprisPlayerPort) -> TrackInfo | None:
        return parse_metadata(await player.get_metadata())

    async def position(self, player: MprisPlayerPort) -> float | None:
        return float(await player.get_position()) / 1_000_000.0

    async def identity(self) -> str:
        return "Test Player"

    async def describe(self, _name: str) -> tuple[str, str, TrackInfo]:
        if self.player_value is None:
            raise LookupError
        return "Test Player", "Playing", parse_metadata(await self.player_value.get_metadata())

    async def subscribe(self, name: str, _callback) -> None:
        self.subscriptions.append(name)


class RecordingResolver:
    def __init__(self, result: LyricsSourceResult | None = None) -> None:
        self.tracks = []
        self.result = result
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.block = False
        self.fail_start = False
        self.fail_close = False
        self.start_calls = 0
        self.close_calls = 0

    def start(self) -> None:
        self.start_calls += 1
        if self.fail_start:
            raise RuntimeError("resolver start failed")
        return None

    async def resolve(self, _session, track, _sources, /):
        self.tracks.append(track)
        self.started.set()
        if self.block:
            await self.release.wait()
        return self.result

    async def resolve_hint(self, _session, _track, _sources, _hint, /):
        return None

    async def resolve_with_diagnostics(self, session, track, sources, /):
        return ResolverLookup(await self.resolve(session, track, sources))

    @property
    def live_source_id(self) -> str:
        return "cider"

    def reset_memory(self) -> None:
        return None

    def set_cache_enabled(self, _enabled: bool) -> None:
        return None

    def set_prefer_best(self, _enabled: bool) -> None:
        return None

    def set_fuzzy(self, _enabled: bool) -> None:
        return None

    async def clear_cache(self) -> None:
        return None

    async def search_cache(self, query: LyricsCacheQuery) -> tuple[LyricsCacheEntry, ...]:
        del query
        return ()

    async def get_cache(self, key: LyricsCacheKey) -> LyricsCacheEntry | None:
        del key
        return None

    async def upsert_cache(
        self,
        artifact: LyricsArtifact,
        *,
        mode: LyricsCacheMode = LyricsCacheMode.MANUAL,
    ) -> CacheWriteResult:
        del mode
        return CacheWriteResult(
            LyricsCacheKey(artifact.provider, artifact.provider_song_id),
            CacheWriteStatus.CREATED,
        )

    async def update_cache(
        self,
        key: LyricsCacheKey,
        artifact: LyricsArtifact,
        *,
        mode: LyricsCacheMode = LyricsCacheMode.MANUAL,
    ) -> CacheWriteResult:
        del artifact, mode
        return CacheWriteResult(key, CacheWriteStatus.UPDATED)

    async def delete_cache(self, key: LyricsCacheKey) -> CacheDeleteResult:
        return CacheDeleteResult(key, CacheDeleteStatus.DELETED)

    async def delete_cache_many(self, keys: tuple[LyricsCacheKey, ...]) -> tuple[CacheDeleteResult, ...]:
        return tuple(CacheDeleteResult(key, CacheDeleteStatus.DELETED) for key in keys)

    async def cancel_inflight(self) -> None:
        return None

    async def close(self) -> None:
        self.close_calls += 1
        if self.fail_close:
            raise RuntimeError("resolver close failed")
        return None


def track_commit(generation: int, title: str = "Song", artist: str = "Artist") -> TrackCommit:
    return TrackCommit(
        generation=generation,
        player_name="org.mpris.MediaPlayer2.test",
        info=TrackInfo(title, artist, "Album", 180.0, f"/{generation}"),
    )


def lyric_result(source_id: str = "netease") -> LyricsSourceResult:
    lines = (LyricLine(0, "L0", 0.0, 5.0, "first", ""), LyricLine(1, "L1", 5.0, 10.0, "second", ""))
    return LyricsSourceResult(
        source_id,
        document=LyricsDocument(
            source_id,
            source_name="Resolved Provider",
            song_id="provider-song-1",
            timing=TimingKind.LINE,
            language="en",
            title="Song",
            artist="Artist",
            duration_s=180.0,
            lines=lines,
        ),
    )


def cider_facts():
    line = LyricLine(0, "L0", 0.0, 5.0, "Song", "")
    document = LyricsDocument("apple-music", timing=TimingKind.LINE, title="Song", artist="Artist", lines=(line,))
    track = TrackIdentity("cider", "cider", stable_id="song-1", title="Song", artist="Artist")
    observation = PlaybackObservation("cider", "cider", track, PlaybackStatus.PLAYING, 1.0, 180.0, 1.0)
    return observation, document


def mpris_provider(
    session: FakeSession,
    resolver: RecordingResolver,
) -> MprisProvider:
    """Build the public MPRIS facade with deterministic boundary fakes."""
    return MprisProvider(
        _display(LyricsState()),
        lyrics_sources=[],
        ownership=SourceOwnershipCoordinator(),
        resolver=resolver,
        playback_adapter=MprisPlaybackAdapter(),
        playback_session=session,
    )


class FakeLyricsSession:
    def __init__(self, *, fail_close: bool = False) -> None:
        self.close_calls = 0
        self.fail_close = fail_close

    async def close(self) -> None:
        self.close_calls += 1
        if self.fail_close:
            raise RuntimeError("HTTP session close failed")


def resolution_session(resolver: RecordingResolver) -> MprisResolutionSession:
    """Build the public MPRIS resolution session with test boundaries."""
    return MprisResolutionSession(
        ownership=SourceOwnershipCoordinator(),
        resolver=resolver,
        playback_adapter=MprisPlaybackAdapter(),
    )


async def test_mpris_provider_rolls_back_playback_when_lyrics_start_fails():
    session = FakeSession(None)
    resolver = RecordingResolver()
    resolver.fail_start = True
    provider = mpris_provider(session, resolver)

    with pytest.raises(RuntimeError, match="resolver start failed"):
        await provider.start()

    assert session.closed is True
    assert session.close_calls == 1


async def test_resolution_start_does_not_start_resolver_when_http_session_fails(monkeypatch):
    from kotonoha.providers import mpris_resolution as resolution_module

    resolver = RecordingResolver()

    def fail_session_creation():
        raise RuntimeError("HTTP session creation failed")

    monkeypatch.setattr(resolution_module, "new_lyrics_session", fail_session_creation)

    with pytest.raises(RuntimeError, match="HTTP session creation failed"):
        await resolution_session(resolver).start()

    assert resolver.start_calls == 0
    assert resolver.close_calls == 0


async def test_resolution_start_closes_http_session_when_resolver_start_fails(monkeypatch):
    from kotonoha.providers import mpris_resolution as resolution_module

    resolver = RecordingResolver()
    resolver.fail_start = True
    http_session = FakeLyricsSession()
    monkeypatch.setattr(resolution_module, "new_lyrics_session", lambda: http_session)

    with pytest.raises(RuntimeError, match="resolver start failed"):
        await resolution_session(resolver).start()

    assert http_session.close_calls == 1


async def test_mpris_provider_attempts_all_cleanup_boundaries_after_failure():
    session = FakeSession(None, fail_close=True)
    resolver = RecordingResolver()
    resolver.fail_close = True
    provider = mpris_provider(session, resolver)
    await provider.start()

    with pytest.raises(RuntimeError, match="session close failed"):
        await provider.stop()

    assert session.close_calls == 1
    assert resolver.close_calls == 1


async def test_playback_coordinator_owns_sampling_and_commits_stable_tracks():
    player = FakePlayer(VALID_METADATA, position=3_000_000)
    session = FakeSession(player)
    commits = []
    samples: list[PlaybackSample] = []
    coordinator = MprisPlaybackCoordinator(
        session=session,
        playback_adapter=MprisPlaybackAdapter(),
        on_sample=samples.append,
        on_commit=commits.append,
    )

    await coordinator.poll_once(now=0.0)
    await coordinator.poll_once(now=0.5)

    assert session.subscriptions == ["org.mpris.MediaPlayer2.test"]
    assert len(commits) == 1
    assert coordinator.current_commit == commits[0]
    assert samples[-1].observation.adapter_id == "mpris"
    assert samples[-1].observation.status is PlaybackStatus.PLAYING
    assert samples[-1].observation.position_s == 3.0


async def test_playback_coordinator_logs_status_changes(caplog):
    player = FakePlayer(VALID_METADATA, position=3_000_000)
    session = FakeSession(player)
    coordinator = MprisPlaybackCoordinator(session=session, playback_adapter=MprisPlaybackAdapter())

    with caplog.at_level(logging.INFO):
        await coordinator.poll_once(now=0.0)
        player.status = "Paused"
        await coordinator.poll_once(now=0.5)

    assert any(
        "MPRIS playback state changed" in record.getMessage()
        and "status=Paused" in record.getMessage()
        for record in caplog.records
    )


async def test_playback_coordinator_closes_its_session_and_poll_task():
    session = FakeSession(None)
    coordinator = MprisPlaybackCoordinator(
        session=session,
        playback_adapter=MprisPlaybackAdapter(),
        poll_interval=0.01,
    )

    await coordinator.start()
    await asyncio.sleep(0)
    await coordinator.stop()
    await coordinator.stop()

    assert session.closed is True


async def test_playback_start_closes_session_when_poll_task_cannot_start(monkeypatch):
    from kotonoha.providers import mpris_playback as playback_module

    session = FakeSession(None)
    coordinator = MprisPlaybackCoordinator(session=session, playback_adapter=MprisPlaybackAdapter())

    def fail_create_task(coroutine, *, name):
        coroutine.close()
        raise RuntimeError(f"cannot create {name}")

    monkeypatch.setattr(playback_module, "create_owned_task", fail_create_task)

    with pytest.raises(RuntimeError, match="cannot create kotonoha-mpris-playback"):
        await coordinator.start()

    assert session.close_calls == 1
    assert session.closed is True


async def test_playback_coordinator_can_restart_after_stop():
    session = FakeSession(None)
    coordinator = MprisPlaybackCoordinator(
        session=session,
        playback_adapter=MprisPlaybackAdapter(),
        poll_interval=0.01,
    )

    await coordinator.start()
    await coordinator.stop()
    assert session.closed is True

    await coordinator.start()
    assert session.closed is False
    await coordinator.stop()


async def test_playback_coordinator_reports_no_player():
    calls: list[float] = []
    coordinator = MprisPlaybackCoordinator(
        session=FakeSession(None),
        playback_adapter=MprisPlaybackAdapter(),
        on_no_player=calls.append,
    )

    await coordinator.poll_once(now=4.0)

    assert calls == [4.0]


def lyrics_coordinator(
    display: DisplayCoordinator,
    *,
    resolver: ResolverPort,
    ownership: SourceOwnershipCoordinator,
) -> MprisLyricsCoordinator:
    """Build the lyric workflow through its public coordinator contract."""
    return MprisLyricsCoordinator(
        display,
        resolver=resolver,
        ownership=ownership,
        playback_adapter=MprisPlaybackAdapter(),
    )


async def test_new_generation_cancels_old_resolution():
    resolver = RecordingResolver()
    resolver.block = True
    coordinator = lyrics_coordinator(
        _display(LyricsState()),
        resolver=resolver,
        ownership=SourceOwnershipCoordinator(),
    )
    coordinator.on_playback_commit(track_commit(1, "A", "Artist A"))
    await resolver.started.wait()

    coordinator.on_playback_commit(track_commit(2, "B", "Artist B"))
    assert coordinator.load_task is not None
    resolver.release.set()
    await coordinator.load_task

    assert [track.title for track in resolver.tracks] == ["A", "B"]
    assert coordinator.current_commit is not None
    assert coordinator.current_commit.info.title == "B"


async def test_mpris_commit_claims_external_ownership_before_resolution_starts():
    ownership = SourceOwnershipCoordinator()
    coordinator = lyrics_coordinator(
        _display(LyricsState()),
        resolver=RecordingResolver(),
        ownership=ownership,
    )

    coordinator.on_playback_commit(track_commit(1))

    # The commit callback runs synchronously; another adapter must not publish in
    # the event-loop gap before the resolver task gets its first turn.
    assert ownership.accepts("cider") is False

    await coordinator.stop()


async def test_mpris_lyrics_stop_resets_state_when_resolution_close_fails(monkeypatch):
    from kotonoha.providers import mpris_resolution as resolution_module

    resolver = RecordingResolver()
    resolver.fail_close = True
    monkeypatch.setattr(resolution_module, "new_lyrics_session", FakeLyricsSession)
    ownership = SourceOwnershipCoordinator()
    coordinator = lyrics_coordinator(_display(LyricsState()), resolver=resolver, ownership=ownership)

    await coordinator.start()
    coordinator.on_playback_commit(track_commit(1))

    with pytest.raises(RuntimeError, match="resolver close failed"):
        await coordinator.stop()

    assert coordinator.current_commit is None
    assert coordinator.content_owner == "none"
    assert ownership.mode == "standalone"


async def test_mpris_transition_claims_external_ownership_before_commit():
    ownership = SourceOwnershipCoordinator()
    coordinator = lyrics_coordinator(
        _display(LyricsState()),
        resolver=RecordingResolver(),
        ownership=ownership,
    )
    commit = track_commit(1)
    observation = PlaybackObservation(
        "mpris",
        "org.mpris.MediaPlayer2.test",
        TrackIdentity("mpris", "test", stable_id="song", title="Song", artist="Artist"),
        PlaybackStatus.PLAYING,
        0.0,
        180.0,
        1.0,
    )

    coordinator.on_playback_sample(PlaybackSample(observation, commit.info, True, commit))

    assert ownership.accepts("cider") is False

    await coordinator.stop()


async def test_external_result_is_projected_to_the_canonical_frame(caplog):
    state = LyricsState()
    coordinator = lyrics_coordinator(
        _display(state),
        resolver=RecordingResolver(lyric_result("lrclib")),
        ownership=SourceOwnershipCoordinator(),
    )
    with caplog.at_level(logging.INFO):
        coordinator.on_playback_commit(track_commit(1))
        assert coordinator.load_task is not None
        await coordinator.load_task

    observation = PlaybackObservation(
        "mpris",
        "org.mpris.MediaPlayer2.test",
        TrackIdentity("mpris", "test", title="Song", artist="Artist", duration_s=180.0),
        PlaybackStatus.PLAYING,
        2.0,
        180.0,
        1.0,
    )
    coordinator.on_playback_sample(PlaybackSample(observation, track_commit(1).info, False, None))

    assert state.frame.state is DisplayState.LYRICS_AVAILABLE
    assert state.frame.document is not None
    assert state.frame.document.source_id == "lrclib"
    assert state.frame.document.source_name == "Resolved Provider"
    assert state.frame.document.song_id == "provider-song-1"
    assert state.frame.document.language == "en"
    assert state.frame.current is not None
    assert any(
        "LYRICS DISPLAY ACTIVE" in record.getMessage()
        and "lyric_source='Resolved Provider'" in record.getMessage()
        and "source_id='lrclib'" in record.getMessage()
        and "playback_source='mpris'" in record.getMessage()
        for record in caplog.records
    )


async def test_mpris_no_lyrics_resolution_publishes_not_found_state():
    state = LyricsState()
    coordinator = lyrics_coordinator(
        _display(state),
        resolver=RecordingResolver(),
        ownership=SourceOwnershipCoordinator(),
    )

    coordinator.on_playback_commit(track_commit(1))
    assert coordinator.load_task is not None
    await coordinator.load_task

    assert state.frame.state is DisplayState.LYRICS_NOT_FOUND
    assert state.frame.track is not None
    assert state.frame.document is None


async def test_mpris_resolution_keeps_a_paused_playback_observation():
    state = LyricsState()
    coordinator = lyrics_coordinator(
        _display(state),
        resolver=RecordingResolver(),
        ownership=SourceOwnershipCoordinator(),
    )
    commit = track_commit(1)
    coordinator.on_playback_commit(commit)
    observation = PlaybackObservation(
        "mpris",
        "org.mpris.MediaPlayer2.test",
        TrackIdentity("mpris", "org.mpris.MediaPlayer2.test", title="Song", artist="Artist"),
        PlaybackStatus.PAUSED,
        4.0,
        180.0,
        1.0,
    )
    coordinator.on_playback_sample(PlaybackSample(observation, commit.info, False, None))

    assert coordinator.load_task is not None
    await coordinator.load_task

    assert state.frame.state is DisplayState.LYRICS_NOT_FOUND
    assert state.frame.is_playing is False
    assert state.frame.current_time == 4.0


async def test_cider_frame_can_take_over_after_a_late_external_miss(caplog: pytest.LogCaptureFixture) -> None:
    """An explicitly enabled Cider source can supply lyrics after a network miss."""
    caplog.set_level(logging.DEBUG)
    resolver = RecordingResolver()
    gate = SourceOwnershipCoordinator()
    state = LyricsState()
    coordinator = lyrics_coordinator(_display(state), resolver=resolver, ownership=gate)
    coordinator.set_lyrics_sources(["netease", "lrclib", "kugou", "cider"])
    coordinator.on_playback_commit(track_commit(1))
    await resolver.started.wait()

    observation, document = cider_facts()
    gate.observe(10, observation, document)
    resolver.release.set()
    assert coordinator.load_task is not None
    await coordinator.load_task

    assert coordinator.content_owner == "live"
    assert gate.accepts(10) is True
    assert state.frame.document is not None
    assert any(
        "lyrics display source transition" in record.getMessage()
        and "current_slot='cider'" in record.getMessage()
        for record in caplog.records
    )
    assert any("LYRICS DISPLAY ACTIVE" in record.getMessage() for record in caplog.records)


async def test_matching_cider_clock_can_drive_external_timeline():
    state = LyricsState()
    gate = SourceOwnershipCoordinator()
    observation, document = cider_facts()
    gate.observe(10, observation, document)
    gate.observe_clock(10, observation.track.track_ref, 7.5, True)
    resolver = RecordingResolver(lyric_result())
    coordinator = lyrics_coordinator(_display(state), resolver=resolver, ownership=gate)
    coordinator.on_playback_commit(track_commit(1))
    assert coordinator.load_task is not None
    await coordinator.load_task

    info = track_commit(1).info
    observation = PlaybackObservation(
        "mpris",
        "org.mpris.MediaPlayer2.test",
        TrackIdentity("mpris", "test", title="Song", artist="Artist", duration_s=180.0),
        PlaybackStatus.PLAYING,
        999.0,
        180.0,
        1.0,
    )
    coordinator.on_playback_sample(PlaybackSample(observation, info, False, None))

    assert state.frame.current_time == pytest.approx(7.5, abs=0.01)


async def test_non_song_is_not_sent_to_the_resolver():
    resolver = RecordingResolver()
    coordinator = lyrics_coordinator(
        _display(LyricsState()),
        resolver=resolver,
        ownership=SourceOwnershipCoordinator(),
    )
    commit = track_commit(1, "Study with Miku - part4 -")
    commit = TrackCommit(commit.generation, commit.player_name, replace_length(commit.info, 7201.0))

    coordinator.on_playback_commit(commit)
    assert coordinator.load_task is not None
    await coordinator.load_task

    assert resolver.tracks == []
    assert coordinator.content_owner == "none"


def replace_length(info: TrackInfo, length_s: float) -> TrackInfo:
    return TrackInfo(info.title, info.artist, info.album, length_s, info.track_id, info.reported_title, info.url)
