from __future__ import annotations

import csv
from collections import Counter
from dataclasses import fields
from pathlib import Path

from fixtures.mpris_titles import CATEGORIES, MPRIS_TITLE_CASES, Fragment, TitleCase

EXPECTED_COUNT = 133


def test_fixture_has_exact_unique_coverage() -> None:
    fixture = [(case.raw_title, case.raw_artist) for case in MPRIS_TITLE_CASES]
    assert len(fixture) == len(MPRIS_TITLE_CASES) == EXPECTED_COUNT
    assert len(set(fixture)) == len(fixture)
    assert any(case.raw_title.endswith(" ") for case in MPRIS_TITLE_CASES)


def test_fixture_matches_the_corpus_it_was_built_from() -> None:
    corpus_path = Path(__file__).parent / "fixtures" / "mpris_titles.tsv"

    with corpus_path.open(encoding="utf-8", newline="") as corpus_file:
        # QUOTE_NONE: real titles start with a quote character ("PINKY UP" MV …), which
        # csv would otherwise read as field quoting and strip.
        rows = list(csv.reader(corpus_file, delimiter="\t", quoting=csv.QUOTE_NONE))

    rows = [row for row in rows if row and not row[0].startswith("#")]
    corpus = [(raw_title, raw_artist) for raw_title, raw_artist, *_ in rows]
    fixture = [(case.raw_title, case.raw_artist) for case in MPRIS_TITLE_CASES]
    assert len(corpus) == len(fixture) == EXPECTED_COUNT
    assert corpus == fixture


def test_records_are_frozen_and_typed() -> None:
    assert all(isinstance(case, TitleCase) for case in MPRIS_TITLE_CASES)
    assert all(type(case).__dataclass_params__.frozen for case in MPRIS_TITLE_CASES)
    assert Fragment.__dataclass_params__.frozen is True
    assert TitleCase.__dataclass_params__.frozen is True
    assert {field.name for field in fields(TitleCase)} >= {
        "raw_title",
        "raw_artist",
        "clean_title",
        "clean_artist",
        "fragments",
        "ambiguous",
        "artist_recovery",
    }


def test_every_row_has_explicit_integrity_fields() -> None:
    assert all(case.category in CATEGORIES for case in MPRIS_TITLE_CASES)
    assert all(case.clean_title for case in MPRIS_TITLE_CASES)
    assert all(case.clean_artist for case in MPRIS_TITLE_CASES)
    assert all(isinstance(case.fragments, tuple) for case in MPRIS_TITLE_CASES)
    assert all(
        fragment.raw and fragment.category in CATEGORIES
        for case in MPRIS_TITLE_CASES
        for fragment in case.fragments
    )


def test_variant_rows_have_variant_evidence() -> None:
    for case in MPRIS_TITLE_CASES:
        if case.category == "variant":
            assert case.fragments, case.raw_title
            assert any(fragment.category == "variant" for fragment in case.fragments)
            assert any(fragment.clean == "" for fragment in case.fragments)


def test_ambiguity_has_reason_and_artist_recovery_is_explicit() -> None:
    assert all(case.ambiguity for case in MPRIS_TITLE_CASES if case.ambiguous)
    assert all(not case.ambiguity for case in MPRIS_TITLE_CASES if not case.ambiguous)
    assert all(isinstance(case.artist_recovery, bool) for case in MPRIS_TITLE_CASES)


def test_category_counts_are_stable() -> None:
    counts = Counter(case.category for case in MPRIS_TITLE_CASES)
    assert sum(counts.values()) == EXPECTED_COUNT
    assert counts == Counter(
        {
            "platform_noise": 36,
            "variant": 41,
            "plain": 16,
            "not_music": 10,
            "descriptor": 11,
            "title_pair": 8,
            "collaboration": 9,
            "alt_title": 2,
        }
    )
