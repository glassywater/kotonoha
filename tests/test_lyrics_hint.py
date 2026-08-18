from pathlib import Path

import pytest

from kotonoha.lyrics.hint import LyricsHint, from_player


@pytest.mark.parametrize(
    ("identity", "bus", "track_id", "url", "expected"),
    [
        ("ElectronNCM", "", "/track/123", "", LyricsHint("netease", "123")),
        ("Qcm", "", "/track/123", "", LyricsHint("netease", "123")),
        ("", "org.mpris.MediaPlayer2.musicfox", "/track/123", "", LyricsHint("netease", "123")),
        ("", "NeteaseCloudMusicGtk4.instance", "/track/123", "", LyricsHint("netease", "123")),
        ("feeluown", "", "", "fuo://netease/songs/123", LyricsHint("netease", "123")),
        ("feeluown", "", "", "fuo://qqmusic/songs/456", LyricsHint("qqmusic", "456")),
        ("YesPlayMusic", "", "", "/trackid/789", LyricsHint("netease", "789")),
        ("unknown", "", "", "file:///tmp/song.flac", LyricsHint("local", local_path=Path("/tmp/song.flac"))),
    ],
)
def test_known_hint_rules(identity, bus, track_id, url, expected):
    assert from_player(identity, bus, track_id, url) == expected


@pytest.mark.parametrize(
    ("identity", "bus", "track_id", "url"),
    [
        ("Other", "", "/track/123", ""),
        ("ElectronNCM", "", "", ""),
        ("feeluown", "", "", "fuo://qqmusic/album/456"),
        ("YesPlayMusic", "", "", "trackid/789"),
        ("unknown", "", "", "https://example.test/song"),
    ],
)
def test_unknown_or_malformed_players_yield_no_hint(identity, bus, track_id, url):
    assert from_player(identity, bus, track_id, url) is None


def test_a_file_uri_naming_another_host_is_not_a_local_path():
    # RFC 8089: only an empty authority or "localhost" names a local file. The
    # authority was dropped, so a player publishing file://remote.example/etc/song
    # chose which local file the overlay opened.
    assert from_player("unknown", "", "", "file://remote.example/etc/song.flac") is None

    hint = from_player("unknown", "", "", "file://localhost/music/song.flac")
    assert hint is not None and hint.local_path == Path("/music/song.flac")
