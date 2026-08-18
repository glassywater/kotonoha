import base64
import json
from typing import cast

import aiohttp

from kotonoha.lyrics import qqmusic


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _response(*, retcode=0, lyric="[00:01.00]line", trans="[00:01.00]translation"):
    body = {"retcode": retcode, "lyric": _b64(lyric), "trans": _b64(trans)}
    return f"MusicJsonCallback({json.dumps(body)})"


def test_parse_response_strips_jsonp_decodes_base64_and_merges_translation():
    payload = qqmusic.parse_response(_response())
    lines = qqmusic.parse_payload(payload)

    assert [line.text for line in lines] == ["line"]
    assert [line.translation for line in lines] == ["translation"]


def test_nonzero_retcode_and_empty_lyric_are_misses():
    assert qqmusic.parse_payload(qqmusic.parse_response(_response(retcode=1))) == ()
    assert qqmusic.parse_payload(qqmusic.parse_response(_response(lyric="", trans=""))) == ()


class _Content:
    """The streaming half of a response, which is what the provider reads.

    The provider caps how much of a body it will buffer, so it reads through
    content.read(limit) rather than text()/json(); a fake that offered only the
    convenience methods would let the cap go untested.
    """

    def __init__(self, body: bytes) -> None:
        self._body = body

    async def read(self, limit: int) -> bytes:
        return self._body[:limit]


class _Response:
    def __init__(self, body):
        self.body = body
        self.content = _Content(body.encode() if isinstance(body, str) else body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def raise_for_status(self):
        return None


class _Session:
    def __init__(self, body):
        self.body = body
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _Response(self.body)

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _Response(self.body)


async def test_fetch_payload_uses_songmid_endpoint_without_network():
    session = _Session(_response())
    payload = await qqmusic.fetch_payload(cast(aiohttp.ClientSession, session), "003aAYrm3GE0Ac")

    assert payload["lyric"] == "[00:01.00]line"
    assert session.calls[0][0] == qqmusic.LYRIC_URL
    assert session.calls[0][1]["params"]["songmid"] == "003aAYrm3GE0Ac"
    assert session.calls[0][1]["headers"] == {"Referer": "https://y.qq.com"}


async def test_song_id_conversion_then_lyric_fetch_uses_recorded_responses():
    detail = {"detail": {"data": {"track_info": {"id": 4830342, "mid": "001OyHbk2MSIi4"}}}}
    session = _Session(json.dumps(detail))
    session.body = _response()

    def post_detail(url, **kwargs):
        session.calls.append((url, kwargs))
        return _Response(json.dumps(detail))

    session.post = post_detail  # ty: ignore[invalid-assignment]
    payload = await qqmusic.fetch_payload_for_song_id(cast(aiohttp.ClientSession, session), "4830342")

    assert payload["lyric"] == "[00:01.00]line"
    assert session.calls[0][0] == qqmusic.DETAIL_URL
    assert session.calls[0][1]["json"]["detail"]["param"] == {"song_id": 4830342}
    assert session.calls[1][1]["params"]["songmid"] == "001OyHbk2MSIi4"
    assert await qqmusic.fetch_song_mid(cast(aiohttp.ClientSession, session), "not-numeric") is None


async def test_an_oversized_response_is_refused_rather_than_buffered():
    # A timeout bounds how long a response may take, not how large it may be: a
    # server that streams steadily holds the connection under the limit while the
    # buffered body grows without end.
    session = _Session("x" * (qqmusic.MAX_RESPONSE_BYTES + 10))

    try:
        await qqmusic.fetch_payload(cast(aiohttp.ClientSession, session), "mid")
    except ValueError as exc:
        assert "size limit" in str(exc)
    else:
        raise AssertionError("an unbounded body was buffered")
