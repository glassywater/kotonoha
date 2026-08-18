"""QQ Music timed-lyrics provider for exact song mids."""

from __future__ import annotations

import base64
import binascii
import json
import time
from collections.abc import Mapping
from typing import Any

import aiohttp

from ..model import LyricLine
from .artifact import LyricsArtifact
from .lrc_parser import merge_translation, parse_lrc
from .match import TrackMetadata

LYRIC_URL = "https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg"
DETAIL_URL = "https://u.y.qq.com/cgi-bin/musicu.fcg"
HEADERS = {"Referer": "https://y.qq.com"}
TIMEOUT = aiohttp.ClientTimeout(total=6.0, connect=3.0)
# A timeout bounds how long a response may take, not how large it may be: a server
# that streams steadily can hold the connection under the limit while the buffered
# body grows without end. Lyrics for one song are a few kilobytes.
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


async def _read_capped(response: aiohttp.ClientResponse) -> bytes:
    """Return the body, refusing one larger than MAX_RESPONSE_BYTES."""
    body = await response.content.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("QQ Music response exceeded the size limit")
    return body


def _decode_lyric(value: object) -> str:
    if not isinstance(value, str) or not value:
        return ""
    try:
        return base64.b64decode(value).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise ValueError("QQ Music lyric is not valid UTF-8 base64") from exc


def parse_payload(payload: Mapping[str, str]) -> tuple[LyricLine, ...]:
    """Return timed lines from a stored payload, merging any translation track."""
    lyric = parse_lrc(payload.get("lyric", ""))
    translation = parse_lrc(payload.get("trans", ""))
    return tuple(merge_translation(lyric, translation) if translation else lyric)


def parse_response(body: str) -> dict[str, str]:
    """Return the lyric and translation fields from the endpoint's JSONP body."""
    prefix = "MusicJsonCallback("
    if not body.startswith(prefix) or not body.endswith(")"):
        raise ValueError("QQ Music lyric response is not JSONP")
    data = json.loads(body[len(prefix) : -1])
    if not isinstance(data, dict):
        raise ValueError("QQ Music lyric response is not an object")
    if data.get("retcode") != 0:
        return {}
    return {
        "lyric": _decode_lyric(data.get("lyric")),
        "trans": _decode_lyric(data.get("trans")),
    }


async def fetch_payload(session: aiohttp.ClientSession, song_mid: str) -> dict[str, str]:
    """Fetch the raw lyric payload for a song mid, as it is cached."""
    params = {
        "songmid": song_mid,
        "pcachetime": str(int(time.time() * 1000)),
        "g_tk": "5381",
        "loginUin": "0",
        "hostUin": "0",
        "inCharset": "utf8",
        "outCharset": "utf-8",
        "notice": "0",
        "platform": "yqq",
        "needNewCode": "0",
    }
    async with session.get(LYRIC_URL, params=params, headers=HEADERS, timeout=TIMEOUT) as response:
        response.raise_for_status()
        body = await _read_capped(response)
    return parse_response(body.decode("utf-8", "replace"))


async def fetch_song_mid(session: aiohttp.ClientSession, song_id: str) -> str | None:
    """Resolve a numeric song id to the mid the lyric endpoint takes, or None."""
    try:
        numeric_id = int(song_id)
    except ValueError:
        return None
    payload: dict[str, Any] = {
        "comm": {
            "g_tk": 5381,
            "uin": 0,
            "format": "json",
            "inCharset": "utf-8",
            "outCharset": "utf-8",
            "notice": 0,
            "platform": "h5",
            "needNewCode": 1,
        },
        "detail": {
            "module": "music.pf_song_detail_svr",
            "method": "get_song_detail",
            "param": {"song_id": numeric_id},
        },
    }
    async with session.post(DETAIL_URL, json=payload, headers=HEADERS, timeout=TIMEOUT) as response:
        response.raise_for_status()
        raw = await _read_capped(response)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"QQ Music song detail response is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("QQ Music song detail response is not an object")
    detail = data.get("detail")
    track_info = detail.get("data", {}).get("track_info") if isinstance(detail, dict) else None
    track_id = track_info.get("id") if isinstance(track_info, dict) else None
    if not isinstance(track_id, int) or track_id <= 0:
        return None
    mid = track_info.get("mid") if isinstance(track_info, dict) else None
    return mid if isinstance(mid, str) and mid else None


async def fetch_payload_for_song_id(session: aiohttp.ClientSession, song_id: str) -> dict[str, str]:
    """Fetch the lyric payload for a numeric song id; empty when it resolves to nothing."""
    song_mid = await fetch_song_mid(session, song_id)
    return await fetch_payload(session, song_mid) if song_mid is not None else {}


async def fetch_lyrics(session: aiohttp.ClientSession, song_mid: str) -> list[LyricLine]:
    """Fetch and parse the timed lines for a song mid."""
    return list(parse_payload(await fetch_payload(session, song_mid)))


async def fetch_artifact(
    session: aiohttp.ClientSession, track: TrackMetadata, *, fuzzy: bool = False
) -> LyricsArtifact | None:
    """QQ Music is id-only; ordinary metadata search is intentionally unsupported."""
    return None
