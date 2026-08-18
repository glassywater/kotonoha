import asyncio

from kotonoha.lyrics.resolver import ResolvedLyrics
from kotonoha.model import LyricLine, LyricsSnapshot
from kotonoha.providers import mpris as mpris_module
from kotonoha.providers.gate import SourceGate
from kotonoha.providers.mpris import PLAYER_IFACE, MprisProvider, TrackCommit, TrackInfo
from kotonoha.state import LyricsState

VALID_METADATA = {
    "xesam:title": "Song",
    "xesam:artist": ["Artist"],
    "xesam:album": "Album",
    "mpris:length": 180_000_000,
    "mpris:trackid": "/track/1",
}


class FakePlayer:
    def __init__(self, metadata, *, position=0, position_error=None):
        self.metadata = metadata
        self.position = position
        self.position_error = position_error

    async def get_playback_status(self):
        return "Playing"

    async def get_metadata(self):
        return self.metadata

    async def get_position(self):
        if self.position_error is not None:
            raise self.position_error
        return self.position


class SequencedMetadataPlayer(FakePlayer):
    def __init__(self, metadata_sequence):
        super().__init__(metadata={})
        self.metadata_sequence = iter(metadata_sequence)

    async def get_metadata(self):
        return next(self.metadata_sequence)


class RecordingResolver:
    def __init__(self, result=None):
        self.tracks = []
        self.result = result

    async def resolve(self, _session, track, _sources):
        self.tracks.append(track)
        return self.result

    async def resolve_hint(self, _session, _track, _sources, _hint):
        return None

    def reset_memory(self):
        return None

    def set_cache_enabled(self, _enabled):
        return None

    def set_prefer_best(self, _enabled):
        return None

    def set_fuzzy(self, _enabled):
        return None

    async def clear_cache(self):
        return None


class BlockingResolver(RecordingResolver):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.cancelled_generations = []

    async def resolve(self, _session, track, _sources):
        self.tracks.append(track)
        if track.title != "A":
            return None
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled_generations.append(1)
            raise


    async def resolve_hint(self, _session, _track, _sources, _hint):
        return None

class DeferredResolver(RecordingResolver):
    def __init__(self, result=None):
        super().__init__(result)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def resolve(self, _session, track, _sources):
        self.tracks.append(track)
        self.started.set()
        await self.release.wait()
        return self.result


    async def resolve_hint(self, _session, _track, _sources, _hint):
        return None

def track_commit(generation, title, artist):
    return TrackCommit(
        generation=generation,
        player_name="org.mpris.MediaPlayer2.test",
        info=TrackInfo(title, artist, "", 180.0, f"/{generation}"),
    )


def prepare_poll(provider, player):
    async def active_player(**_kwargs):
        return player, "org.mpris.MediaPlayer2.test"

    async def subscribed(_name):
        return None

    provider._active_player = active_player
    provider._ensure_subscribed = subscribed


def _wire_players(provider, players, monkeypatch):
    """players: {bus_name: (player_obj, status, TrackInfo)}."""

    async def fake_list(_bus):
        return sorted(players)

    async def safe_iface(name):
        return players[name][0]

    async def safe_status(player):
        return next(status for _p, status, _i in players.values() if _p is player)

    async def safe_info(player):
        return next(info for _p, _s, info in players.values() if _p is player)

    monkeypatch.setattr(mpris_module, "list_players", fake_list)
    provider._bus = object()
    provider._safe_iface = safe_iface
    provider._safe_status = safe_status
    provider._safe_info = safe_info


async def test_active_player_prefers_complete_metadata_over_alphabetical(monkeypatch):
    # Chrome sorts first but reports an empty artist; PBI has the real artist.
    chromium = ("chrome", "Playing", TrackInfo("Song - YouTube", "", "", 180.0, "/c"))
    pbi = ("pbi", "Playing", TrackInfo("Song", "Artist", "Album", 180.0, "/p"))
    players = {
        "org.mpris.MediaPlayer2.chromium.instance1": chromium,
        "org.mpris.MediaPlayer2.plasma-browser-integration": pbi,
    }
    provider = MprisProvider(LyricsState(), resolver=RecordingResolver())
    _wire_players(provider, players, monkeypatch)

    result = await provider._active_player()

    assert result is not None
    assert result[1] == "org.mpris.MediaPlayer2.plasma-browser-integration"
    assert provider._current_name == "org.mpris.MediaPlayer2.plasma-browser-integration"


async def test_active_player_prefers_player_that_started_recently(monkeypatch):
    old = ("old", "Playing", TrackInfo("Song", "Artist", "Album", 180.0, "/old"))
    new = ("new", "Playing", TrackInfo("Song", "Artist", "Album", 180.0, "/new"))
    players = {"org.mpris.MediaPlayer2.old": old}
    provider = MprisProvider(LyricsState(), resolver=RecordingResolver())
    _wire_players(provider, players, monkeypatch)

    first = await provider._active_player(now=0.0)
    assert first is not None
    assert first[1] == "org.mpris.MediaPlayer2.old"

    players["org.mpris.MediaPlayer2.new"] = new
    result = await provider._active_player(now=20.0)

    assert result is not None
    assert result[1] == "org.mpris.MediaPlayer2.new"


async def test_active_player_keeps_current_when_new_player_is_within_recency_margin(monkeypatch):
    old = ("old", "Playing", TrackInfo("Song", "Artist", "Album", 180.0, "/old"))
    new = ("new", "Playing", TrackInfo("Song", "Artist", "Album", 180.0, "/new"))
    players = {"org.mpris.MediaPlayer2.old": old}
    provider = MprisProvider(LyricsState(), resolver=RecordingResolver())
    _wire_players(provider, players, monkeypatch)

    first = await provider._active_player(now=0.0)
    assert first is not None
    assert first[1] == "org.mpris.MediaPlayer2.old"

    players["org.mpris.MediaPlayer2.new"] = new
    result = await provider._active_player(now=mpris_module.RECENT_PLAYER_MARGIN / 2)

    assert result is not None
    assert result[1] == "org.mpris.MediaPlayer2.old"


async def test_active_player_lock_beats_recently_started_rival(monkeypatch):
    locked = ("locked", "Playing", TrackInfo("Song", "", "", 180.0, "/locked"))
    rival = ("rival", "Playing", TrackInfo("Song", "Artist", "Album", 180.0, "/rival"))
    players = {"org.mpris.MediaPlayer2.locked": locked}
    provider = MprisProvider(LyricsState(), resolver=RecordingResolver())
    provider.set_player_lock("org.mpris.MediaPlayer2.locked")
    _wire_players(provider, players, monkeypatch)

    first = await provider._active_player(now=0.0)
    assert first is not None
    assert first[1] == "org.mpris.MediaPlayer2.locked"

    players["org.mpris.MediaPlayer2.rival"] = rival
    result = await provider._active_player(now=20.0)

    assert result is not None
    assert result[1] == "org.mpris.MediaPlayer2.locked"


async def test_active_player_drops_recency_stamp_for_vanished_player(monkeypatch):
    vanished = ("vanished", "Playing", TrackInfo("Song", "Artist", "Album", 180.0, "/vanished"))
    players = {"org.mpris.MediaPlayer2.vanished": vanished}
    provider = MprisProvider(LyricsState(), resolver=RecordingResolver())
    _wire_players(provider, players, monkeypatch)

    result = await provider._active_player(now=0.0)
    assert result is not None
    assert "org.mpris.MediaPlayer2.vanished" in provider._playing_since

    players.clear()
    assert await provider._active_player(now=20.0) is None
    assert "org.mpris.MediaPlayer2.vanished" not in provider._playing_since


async def test_active_player_lock_beats_more_complete_rival(monkeypatch):
    locked = ("locked", "Playing", TrackInfo("Song", "", "", 180.0, "/locked"))
    rival = ("rival", "Playing", TrackInfo("Song", "Artist", "Album", 180.0, "/rival"))
    players = {
        "org.mpris.MediaPlayer2.locked": locked,
        "org.mpris.MediaPlayer2.rival": rival,
    }
    provider = MprisProvider(LyricsState(), resolver=RecordingResolver())
    provider.set_player_lock("org.mpris.MediaPlayer2.locked")
    _wire_players(provider, players, monkeypatch)

    result = await provider._active_player()

    assert result is not None
    assert result[1] == "org.mpris.MediaPlayer2.locked"


async def test_active_player_absent_lock_falls_back_to_automatic(monkeypatch):
    rival = ("rival", "Playing", TrackInfo("Song", "Artist", "Album", 180.0, "/rival"))
    players = {"org.mpris.MediaPlayer2.rival": rival}
    provider = MprisProvider(LyricsState(), resolver=RecordingResolver())
    provider.set_player_lock("org.mpris.MediaPlayer2.closed")
    _wire_players(provider, players, monkeypatch)

    result = await provider._active_player()

    assert result is not None
    assert result[1] == "org.mpris.MediaPlayer2.rival"


async def test_active_player_falls_back_to_only_source(monkeypatch):
    only = ("chrome", "Playing", TrackInfo("Song - YouTube", "", "", 180.0, "/c"))
    players = {"org.mpris.MediaPlayer2.chromium.instance1": only}
    provider = MprisProvider(LyricsState(), resolver=RecordingResolver())
    _wire_players(provider, players, monkeypatch)

    result = await provider._active_player()

    assert result is not None
    assert result[1] == "org.mpris.MediaPlayer2.chromium.instance1"


async def test_position_failure_does_not_block_lyric_resolution():
    player = FakePlayer(metadata=VALID_METADATA, position_error=RuntimeError("unsupported"))
    resolver = RecordingResolver()
    provider = MprisProvider(LyricsState(), resolver=resolver, poll_interval=0.01)
    prepare_poll(provider, player)

    await provider._poll_once(now=0.0)
    await provider._poll_once(now=0.5)
    assert provider._load_task is not None
    await provider._load_task

    assert resolver.tracks[0].title == "Song"


async def test_empty_metadata_never_reaches_resolver():
    resolver = RecordingResolver()
    provider = MprisProvider(LyricsState(), resolver=resolver)
    prepare_poll(provider, FakePlayer(metadata={"mpris:trackid": "/track/1"}))

    await provider._poll_once(now=0.0)
    await provider._poll_once(now=1.0)

    assert resolver.tracks == []
    assert provider._load_task is None


async def test_metadata_changed_during_sample_is_discarded():
    mixed = dict(VALID_METADATA, **{"xesam:artist": ["Old Artist"]})
    player = SequencedMetadataPlayer([mixed, VALID_METADATA, VALID_METADATA, VALID_METADATA])
    resolver = RecordingResolver()
    provider = MprisProvider(LyricsState(), resolver=resolver)
    prepare_poll(provider, player)

    await provider._poll_once(now=0.0)
    await provider._poll_once(now=0.5)

    assert resolver.tracks == []


async def test_duration_drift_during_metadata_sample_does_not_block_resolution():
    samples = [
        dict(VALID_METADATA, **{"mpris:length": duration})
        for duration in (180_000_000, 181_000_000, 182_000_000, 183_000_000)
    ]
    player = SequencedMetadataPlayer(samples)
    resolver = RecordingResolver()
    provider = MprisProvider(LyricsState(), resolver=resolver)
    prepare_poll(provider, player)

    await provider._poll_once(now=0.0)
    await provider._poll_once(now=0.5)
    assert provider._load_task is not None
    await provider._load_task

    assert len(resolver.tracks) == 1


def test_metadata_signal_only_wakes_sampler():
    provider = MprisProvider(LyricsState(), resolver=RecordingResolver())
    provider._subscribed_name = "org.mpris.MediaPlayer2.test"

    provider._on_props_changed(PLAYER_IFACE, {"Metadata": object()}, [])

    assert provider._poll_wakeup.is_set()
    assert provider._load_task is None


async def test_new_generation_cancels_old_fetch():
    resolver = BlockingResolver()
    state = LyricsState()
    provider = MprisProvider(state, resolver=resolver)
    provider._schedule_load(track_commit(1, "A", "Artist A"))
    await resolver.started.wait()
    provider._schedule_load(track_commit(2, "B", "Artist B"))
    assert provider._load_task is not None
    await provider._load_task

    assert resolver.cancelled_generations == [1]
    assert state.snapshot.title == "B"


async def test_cider_disconnect_forces_ordered_resolution_again():
    resolver = RecordingResolver()
    gate = SourceGate()
    state = LyricsState()
    provider = MprisProvider(state, resolver=resolver, gate=gate)
    provider._current_commit = track_commit(1, "Song", "Artist")
    provider._content_owner = "cider"
    gate.observe_snapshot(10, LyricsSnapshot(found=True, title="Song", artist="Artist"))
    gate.select_cider(10)
    gate.drop_client(10)

    provider._ensure_content_owner()
    assert provider._load_task is not None
    await provider._load_task

    assert len(resolver.tracks) == 1


async def test_late_cider_snapshot_takes_over_after_ordered_miss():
    resolver = DeferredResolver()
    gate = SourceGate()
    state = LyricsState()
    provider = MprisProvider(state, resolver=resolver, gate=gate)
    provider._schedule_load(track_commit(1, "Song", "Artist"))
    await resolver.started.wait()

    snapshot = LyricsSnapshot(found=True, title="Song", artist="Artist")
    gate.observe_snapshot(10, snapshot)
    resolver.release.set()
    assert provider._load_task is not None
    await provider._load_task

    assert provider._content_owner == "cider"
    assert gate.accepts(10) is True
    assert state.snapshot is snapshot


async def test_late_higher_priority_cider_beats_lower_external_result():
    resolver = DeferredResolver(ResolvedLyrics(source="netease", lines=()))
    gate = SourceGate()
    state = LyricsState()
    provider = MprisProvider(
        state,
        resolver=resolver,
        gate=gate,
        lyrics_sources=["cider", "netease"],
    )
    provider._schedule_load(track_commit(1, "Song", "Artist"))
    await resolver.started.wait()

    snapshot = LyricsSnapshot(found=True, title="Song", artist="Artist")
    gate.observe_snapshot(10, snapshot)
    resolver.release.set()
    assert provider._load_task is not None
    await provider._load_task

    assert provider._content_owner == "cider"
    assert state.snapshot is snapshot


async def test_external_result_uses_actual_provider_label():
    state = LyricsState()
    resolver = RecordingResolver(ResolvedLyrics(source="lrclib", lines=()))
    provider = MprisProvider(state, resolver=resolver)
    provider._schedule_load(track_commit(1, "Song", "Artist"))
    assert provider._load_task is not None
    await provider._load_task

    provider._emit(track_commit(1, "Song", "Artist").info, 0.0, True)
    assert state.snapshot.provider == "MPRIS:lrclib"


async def test_cumulative_position_offset_realigns_the_sweep():
    lines = (
        LyricLine(0, "L0", 0.0, 5.0, "first", ""),
        LyricLine(1, "L1", 5.0, 10.0, "second", ""),
    )
    state = LyricsState()
    resolver = RecordingResolver(ResolvedLyrics(source="netease", lines=lines))
    provider = MprisProvider(state, resolver=resolver)
    # Player reports a cumulative playlist position of 507s; the song started at 500s.
    prepare_poll(provider, FakePlayer(metadata=VALID_METADATA, position=507_000_000))

    await provider._poll_once(now=0.0)
    await provider._poll_once(now=0.5)
    assert provider._load_task is not None
    await provider._load_task
    provider._song_offset = 500.0  # captured from the track-transition
    await provider._poll_once(now=1.0)

    # song-relative time = 507 - 500 = 7s -> the second line, not stuck at the end.
    assert state.snapshot.current is not None
    assert state.snapshot.current.text == "second"
    assert state.snapshot.current_time == 7.0


async def test_matching_cider_tick_drives_external_line_selection():
    lines = (
        LyricLine(0, "L0", 0.0, 5.0, "first", ""),
        LyricLine(1, "L1", 5.0, 10.0, "second", ""),
    )
    state = LyricsState()
    gate = SourceGate()
    gate.observe_snapshot(10, LyricsSnapshot(found=False, title="Song", artist="Artist"))
    gate.observe_tick(10, 7.5, True)
    resolver = RecordingResolver(ResolvedLyrics(source="netease", lines=lines))
    provider = MprisProvider(state, resolver=resolver, gate=gate)
    prepare_poll(provider, FakePlayer(metadata=VALID_METADATA, position=999_000_000))

    await provider._poll_once(now=0.0)
    await provider._poll_once(now=0.5)
    assert provider._load_task is not None
    await provider._load_task
    await provider._poll_once(now=1.0)

    assert state.snapshot.current is not None
    assert state.snapshot.current.text == "second"
    assert state.snapshot.current_time == 7.5


async def test_matching_cider_duration_corrects_mpris_search_metadata():
    gate = SourceGate()
    gate.observe_snapshot(
        10,
        LyricsSnapshot(
            found=False,
            title="Song",
            artist="Artist",
            album="Album",
            duration_s=194.222,
        ),
    )
    gate.observe_tick(10, 50.0, True)
    resolver = RecordingResolver()
    provider = MprisProvider(LyricsState(), resolver=resolver, gate=gate)
    provider._schedule_load(
        TrackCommit(
            generation=1,
            player_name="org.mpris.MediaPlayer2.chromium.test",
            info=TrackInfo("Song", "Artist", "Album", 305.059159, "/track/1"),
        )
    )
    assert provider._load_task is not None
    await provider._load_task

    assert resolver.tracks[0].duration_s == 194.222


async def test_late_position_reset_corrects_offset_without_reload():
    lines = (
        LyricLine(0, "L0", 0.0, 5.0, "first", ""),
        LyricLine(1, "L1", 5.0, 10.0, "second", ""),
    )
    state = LyricsState()
    resolver = RecordingResolver(ResolvedLyrics(source="netease", lines=lines))
    provider = MprisProvider(state, resolver=resolver)
    player = FakePlayer(metadata=VALID_METADATA, position=21_125_000)
    prepare_poll(provider, player)

    await provider._poll_once(now=0.0)
    await provider._poll_once(now=0.5)
    assert provider._load_task is not None
    await provider._load_task

    b_metadata = {
        "xesam:title": "SongB",
        "xesam:artist": ["ArtistB"],
        "xesam:album": "AlbumB",
        "mpris:length": 180_000_000,
        "mpris:trackid": "/track/B",
    }
    player.metadata = b_metadata
    await provider._poll_once(now=1.0)
    await provider._poll_once(now=2.0)
    assert provider._load_task is not None
    await provider._load_task
    assert provider._song_offset == 21.125
    assert provider._calibration_offset == 21.125

    player.position = 500_000
    await provider._poll_once(now=2.2)

    assert provider._song_offset == 0.0
    assert len(resolver.tracks) == 2
    assert state.snapshot.current is not None
    assert state.snapshot.current.text == "first"


async def test_cumulative_player_not_miscalibrated_by_normal_advance():
    lines = (
        LyricLine(0, "L0", 0.0, 5.0, "first", ""),
        LyricLine(1, "L1", 5.0, 10.0, "second", ""),
    )
    state = LyricsState()
    resolver = RecordingResolver(ResolvedLyrics(source="netease", lines=lines))
    provider = MprisProvider(state, resolver=resolver)
    player = FakePlayer(metadata=VALID_METADATA, position=500_000_000)
    prepare_poll(provider, player)

    await provider._poll_once(now=0.0)
    await provider._poll_once(now=0.5)
    assert provider._load_task is not None
    await provider._load_task

    b_metadata = {
        "xesam:title": "SongB",
        "xesam:artist": ["ArtistB"],
        "xesam:album": "AlbumB",
        "mpris:length": 180_000_000,
        "mpris:trackid": "/track/B",
    }
    player.metadata = b_metadata
    player.position = 500_500_000
    await provider._poll_once(now=1.0)
    player.position = 501_000_000
    await provider._poll_once(now=2.0)
    assert provider._load_task is not None
    await provider._load_task

    player.position = 507_000_000
    await provider._poll_once(now=2.5)

    assert provider._song_offset == 500.5
    assert state.snapshot.current is not None
    assert state.snapshot.current.text == "second"


async def test_late_reset_during_resolving_corrects_offset():
    lines = (
        LyricLine(0, "L0", 0.0, 5.0, "first", ""),
        LyricLine(1, "L1", 5.0, 10.0, "second", ""),
    )

    state = LyricsState()
    resolver = DeferredResolver(ResolvedLyrics(source="netease", lines=lines))
    provider = MprisProvider(state, resolver=resolver)
    player = FakePlayer(metadata=VALID_METADATA, position=21_125_000)
    prepare_poll(provider, player)

    await provider._poll_once(now=0.0)
    await provider._poll_once(now=0.5)
    await resolver.started.wait()
    resolver.release.set()
    assert provider._load_task is not None
    await provider._load_task

    b_metadata = {
        "xesam:title": "SongB",
        "xesam:artist": ["ArtistB"],
        "xesam:album": "AlbumB",
        "mpris:length": 180_000_000,
        "mpris:trackid": "/track/B",
    }
    player.metadata = b_metadata
    await provider._poll_once(now=1.0)
    await provider._poll_once(now=2.0)
    assert provider._content_owner == "resolving"
    assert provider._song_offset == 21.125

    player.position = 500_000
    await provider._poll_once(now=2.2)
    assert provider._song_offset == 0.0

    resolver.release.set()
    assert provider._load_task is not None
    await provider._load_task
    await provider._poll_once(now=2.3)
    assert state.snapshot.current is not None
    assert state.snapshot.current.text == "first"


class _Variant:
    """What dbus hands back for a single property read: a Variant, not an a{sv} map."""

    def __init__(self, value):
        self.value = value


class _PlayerProperties:
    def __init__(self, identity, status, metadata):
        self.identity = _Variant(identity)
        self.status = _Variant(status)
        self.metadata = _Variant(metadata)

    async def call_get(self, _interface, _property):
        return self.identity

    async def call_get_all(self, _interface):
        return {
            "PlaybackStatus": self.status,
            "Metadata": self.metadata,
        }


class _PlayerObject:
    def __init__(self, properties):
        self.properties = properties

    def get_interface(self, interface):
        if interface == "org.freedesktop.DBus.Properties":
            return self.properties
        raise AssertionError(interface)


class _DiscoveryBus:
    def __init__(self, players):
        self.players = players

    def get_proxy_object(self, name, _path, _introspection):
        return _PlayerObject(self.players[name])


async def test_available_players_reports_track_status_and_automatic_choice(monkeypatch):
    playing_metadata = {
        "xesam:title": "Song",
        "xesam:artist": ["Artist"],
        "mpris:trackid": "/playing",
    }
    idle_metadata = {"mpris:trackid": "/idle"}
    players = {
        "org.mpris.MediaPlayer2.firefox.instance_1_298": _PlayerProperties(
            "Mozilla firefox", "Playing", playing_metadata
        ),
        "org.mpris.MediaPlayer2.plasma-browser-integration": _PlayerProperties(
            "Mozilla Firefox", "Stopped", idle_metadata
        ),
    }
    async def list_discovered_players(_bus):
        return _names(players)

    monkeypatch.setattr(mpris_module, "list_players", list_discovered_players)
    provider = MprisProvider(LyricsState(), resolver=RecordingResolver())
    provider._bus = _DiscoveryBus(players)

    result = await provider.available_players()

    assert result[0].title == "Song"
    assert result[0].artist == "Artist"
    assert result[0].playback_status == "Playing"
    assert result[0].automatic is True
    assert result[1].title == ""
    assert result[1].playback_status == "Stopped"
    assert result[1].automatic is False


def _names(players):
    return sorted(players)


async def test_player_identity_is_unwrapped_from_its_variant():
    # The metadata unwrapper takes a dict; a single property arrives wrapped on its
    # own, and passing it there rendered every player in the picker as "{}".
    from kotonoha.players import PlayerInfo

    identity = _Variant("ElectronNCM")
    assert str(getattr(identity, "value", identity) or "") == "ElectronNCM"
    assert PlayerInfo("org.mpris.MediaPlayer2.ElectronNCM", "ElectronNCM").identity == "ElectronNCM"


async def test_available_players_unwraps_the_identity_variant_through_the_bus(monkeypatch):
    # The Variant fix is only worth as much as the call chain around it: a single
    # property comes back as a Variant, not the a{sv} map the metadata unwrapper
    # takes, and passing it there made every player show as "{}".
    from dbus_fast import Variant

    from kotonoha.providers import mpris as provider_module

    class _Props:
        async def call_get(self, interface: str, name: str) -> object:
            assert (interface, name) == ("org.mpris.MediaPlayer2", "Identity")
            return Variant("s", "ElectronNCM")

        async def call_get_all(self, interface: str) -> dict[str, object]:
            # The player interface, read for the row detail; empty is a valid answer.
            return {}

    class _Obj:
        def get_interface(self, name: str) -> _Props:
            assert name == "org.freedesktop.DBus.Properties"
            return _Props()

    class _Bus:
        def get_proxy_object(self, name: str, path: str, introspection: object) -> _Obj:
            return _Obj()

    monkeypatch.setattr(
        provider_module, "list_players", _async_return(["org.mpris.MediaPlayer2.ElectronNCM"])
    )
    provider = MprisProvider(LyricsState())
    provider._bus = _Bus()

    players = await provider.available_players()

    assert [(p.bus_name, p.identity) for p in players] == [
        ("org.mpris.MediaPlayer2.ElectronNCM", "ElectronNCM")
    ]


def _async_return(value):
    async def _call(_bus):
        return value

    return _call
async def test_non_song_never_reaches_the_resolver():
    # The gate has to be wired into the load path, not merely defined: a 14-hour
    # compilation must not be sent to every lyric provider.
    compilation = dict(
        VALID_METADATA,
        **{"xesam:title": "Study with Miku - part4 -", "mpris:length": 5_040_000_000},
    )
    resolver = RecordingResolver()
    provider = MprisProvider(LyricsState(), resolver=resolver, poll_interval=0.01)
    prepare_poll(provider, FakePlayer(metadata=compilation))

    await provider._poll_once(now=0.0)
    await provider._poll_once(now=0.5)
    if provider._load_task is not None:
        await provider._load_task

    assert resolver.tracks == []


async def test_an_ordinary_song_still_reaches_the_resolver():
    resolver = RecordingResolver()
    provider = MprisProvider(LyricsState(), resolver=resolver, poll_interval=0.01)
    prepare_poll(provider, FakePlayer(metadata=VALID_METADATA))

    await provider._poll_once(now=0.0)
    await provider._poll_once(now=0.5)
    assert provider._load_task is not None
    await provider._load_task

    assert [track.title for track in resolver.tracks] == ["Song"]


async def test_an_over_long_cider_duration_still_skips_the_lookup():
    # The browser reports no length while Cider knows the real one. Running the
    # gate on the MPRIS value first let a two-hour stream through, and the resolver
    # then received the 7201s duration anyway — the very content this gate exists
    # to keep off the providers.
    resolver = RecordingResolver()
    gate = SourceGate()
    state = LyricsState()
    provider = MprisProvider(state, resolver=resolver, gate=gate)
    commit = track_commit(1, "Song", "Artist")
    provider._current_commit = commit
    gate.observe_snapshot(10, LyricsSnapshot(found=False, title="Song", artist="Artist", duration_s=7201.0))
    gate.observe_tick(10, 0.0, True)

    await provider._load_song(commit)

    assert resolver.tracks == [], "a 7201s stream was sent to the lyric providers"
    assert provider._content_owner == "none"


async def test_a_repaired_commit_keeps_the_player_identity():
    # A commit that arrives behind the current generation is rebuilt with a fresh
    # one. Reconstructing it positionally dropped player_identity, so a recognised
    # player stopped producing an exact hint and fell back to matching on the title.
    class HintRecorder(RecordingResolver):
        def __init__(self):
            super().__init__()
            self.hints = []

        async def resolve_hint(self, _session, _track, _sources, _hint):
            self.hints.append(_hint)
            return None

    resolver = HintRecorder()
    provider = MprisProvider(LyricsState(), resolver=resolver)
    info = TrackInfo("Song", "Artist", "", 180.0, "/track/12345")
    provider._current_commit = TrackCommit(2, "org.mpris.MediaPlayer2.x", info, None, "ElectronNCM")

    provider._schedule_load(TrackCommit(1, "org.mpris.MediaPlayer2.x", info, None, "ElectronNCM"))
    assert provider._load_task is not None
    await provider._load_task

    assert resolver.hints, "the repaired commit produced no exact hint at all"
    assert resolver.hints[-1].provider == "netease"
    assert resolver.hints[-1].song_id == "12345"


class _DetailFailingProperties(_PlayerProperties):
    async def call_get_all(self, _interface):
        raise RuntimeError("optional player detail unavailable")


async def test_a_player_whose_detail_read_fails_still_appears_in_the_picker(monkeypatch):
    # Identity and detail shared one try, so a player that answered its name but
    # not its status vanished from the list and could not be selected at all.
    players = {
        "org.mpris.MediaPlayer2.vlc": _DetailFailingProperties("VLC", "Playing", {}),
    }

    async def list_discovered_players(_bus):
        return _names(players)

    monkeypatch.setattr(mpris_module, "list_players", list_discovered_players)
    provider = MprisProvider(LyricsState(), resolver=RecordingResolver())
    provider._bus = _DiscoveryBus(players)

    result = await provider.available_players()

    assert [p.identity for p in result] == ["VLC"]
    assert result[0].playback_status == ""


async def test_the_picker_marks_the_player_the_poll_would_follow(monkeypatch):
    # The picker carried its own copy of the selection policy which ordered the last
    # two fallbacks the other way round: a Playing player reporting no metadata beat
    # a Paused one that reports a track, while the poll chose the opposite.
    players = {
        "org.mpris.MediaPlayer2.a-playing-empty": _PlayerProperties("Empty", "Playing", {}),
        "org.mpris.MediaPlayer2.b-paused-song": _PlayerProperties(
            "Paused", "Paused", {"xesam:title": "Song", "xesam:artist": ["Artist"]}
        ),
    }

    async def list_discovered_players(_bus):
        return _names(players)

    monkeypatch.setattr(mpris_module, "list_players", list_discovered_players)
    provider = MprisProvider(LyricsState(), resolver=RecordingResolver())
    provider._bus = _DiscoveryBus(players)

    result = await provider.available_players()

    marked = [p.bus_name for p in result if p.automatic]
    assert marked == ["org.mpris.MediaPlayer2.b-paused-song"], (
        "the picker marked a player the poll would not follow"
    )
