"""Load timed lyrics from a local audio file or its metadata."""

from __future__ import annotations

from pathlib import Path

from ..model import LyricLine
from .lrc_parser import parse_lrc


def load_local_lyrics(audio_path: Path) -> list[LyricLine]:
    """Return timed lines for a local audio file from the first source that has them.

    Two sources, tried in order: an LRC file sitting beside the audio, then the
    lyrics embedded in the audio's own tags. The name says which job this is,
    because a function called load_sidecar that also parses tags left every caller
    having to know that its name described half of what it did.
    """
    lines = _load_sidecar(audio_path)
    return lines if lines else _load_embedded(audio_path)


def _load_sidecar(audio_path: Path) -> list[LyricLine]:
    """Return timed lines from the LRC file adjacent to the audio file."""
    if not audio_path.name:
        # A player publishing xesam:url = "file:///" reaches here as Path("/"), and
        # with_suffix raises ValueError on a path with no name — outside the OSError
        # handled below. Nothing adjacent to a root can be a sidecar.
        return []
    sidecar = audio_path.with_suffix(".lrc")
    audio_directory = audio_path.parent.resolve()

    try:
        if sidecar.resolve().parent != audio_directory:
            raw = None
        else:
            raw = sidecar.read_bytes()
    except OSError:
        raw = None

    if raw is None:
        return []
    for encoding in ("utf-8", "gb18030"):
        try:
            lines = parse_lrc(raw.decode(encoding))
            if lines:
                return lines
        except UnicodeDecodeError:
            continue
    return []


def _load_embedded(audio_path: Path) -> list[LyricLine]:
    try:
        # Optional: the feature exists only where the user installed it, so the
        # type checker must not treat an absent import as an error.
        import mutagen  # ty: ignore[unresolved-import]
    except ImportError:
        return []

    try:
        audio = mutagen.File(audio_path)
        if audio is None or audio.tags is None:
            return []
        for text in _embedded_texts(audio.tags):
            lines = parse_lrc(text)
            if lines:
                return lines
    except (AttributeError, KeyError, IndexError, OSError, TypeError, ValueError, mutagen.MutagenError):
        return []
    return []


def _embedded_texts(tags: object) -> list[str]:
    """Return candidate text values from the supported mutagen tag shapes."""
    candidates: list[str] = []

    getall = getattr(tags, "getall", None)
    if callable(getall):
        for frame in getall("USLT"):
            text = getattr(frame, "text", None)
            if isinstance(text, str):
                candidates.append(text)

    get = getattr(tags, "get", None)
    if not callable(get):
        return candidates

    # Vorbis comments (FLAC, Ogg) accept only printable ASCII keys and raise
    # ValueError on anything else, so probing the MP4 key on a FLAC threw before
    # the LYRICS value already found here could be returned — every FLAC with
    # embedded lyrics came back empty. Each lookup is therefore isolated.
    for key in ("LYRICS", "UNSYNCEDLYRICS", "\u00a9lyr", b"\xa9lyr"):
        try:
            values = get(key, [])
        except (KeyError, TypeError, ValueError):
            continue
        if isinstance(values, str):
            values = [values]
        if isinstance(values, (list, tuple)):
            candidates.extend(value for value in values if isinstance(value, str))

    return candidates
