import base64
import zlib

from kotonoha.lyrics.krc_parser import KRC_XOR_KEY, parse_krc


def _krc_body(text: str) -> bytes:
    compressed = zlib.compress(text.encode("utf-8"))
    encrypted = bytes(value ^ KRC_XOR_KEY[index % len(KRC_XOR_KEY)] for index, value in enumerate(compressed))
    return b"krc1" + encrypted


def test_parse_krc_decodes_fixture_and_makes_word_times_absolute():
    body = _krc_body("[1200,1000]<0,300,0>先<300,400,0>唱<700,300,0>歌\n")

    lines = parse_krc(body)

    assert len(lines) == 1
    assert lines[0].text == "先唱歌"
    assert lines[0].start == 1.2
    assert lines[0].end == 2.2
    assert [(word.start, word.end) for word in lines[0].words] == [
        (1.2, 1.5),
        (1.5, 1.9),
        (1.9, 2.2),
    ]


def test_parse_krc_rejects_undecodable_body():
    assert parse_krc(base64.b64decode(base64.b64encode(b"not krc"))) == []
