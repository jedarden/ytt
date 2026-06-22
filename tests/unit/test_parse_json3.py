"""Unit tests for ytt.parse_json3 — rolling-caption dedup (plan: Phase 2).

Coverage:
- Manual (non-ASR) track: straight concat, no dedup.
- Rolling ASR track: no doubling + matches pre-verified reference output.
- aAppend / pAppend spacing rules.
- Empty events filtered out.
- Primary dedup (window coverage).
- Prefix-check secondary dedup.
- Formatting-only events (\\n) skipped.
- Empty event dict handled gracefully.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from ytt.parse_json3 import _segs_to_text, parse_json3
from ytt.models import Segment

FIXTURES = pathlib.Path(__file__).parent.parent / "fixtures"


# ---------------------------------------------------------------------------
# _segs_to_text helpers
# ---------------------------------------------------------------------------

class TestSegsToText:
    def test_simple_concat(self):
        segs = [{"utf8": "Hello"}, {"utf8": " world"}]
        assert _segs_to_text(segs) == "Hello world"

    def test_adds_space_when_missing(self):
        """Space is inserted at a junction when neither end already has one."""
        segs = [{"utf8": "Hello"}, {"utf8": "world"}]
        assert _segs_to_text(segs) == "Hello world"

    def test_no_double_space(self):
        """If utf8 already starts with a space, don't add another."""
        segs = [{"utf8": "Hello"}, {"utf8": " world"}]
        assert _segs_to_text(segs) == "Hello world"

    def test_aAppend_no_space(self):
        """aAppend=1 joins without a space (word continuation)."""
        segs = [{"utf8": "watch"}, {"utf8": "ing", "aAppend": 1}]
        assert _segs_to_text(segs) == "watching"

    def test_pAppend_no_space(self):
        """pAppend=1 attaches punctuation without a space."""
        segs = [{"utf8": "Hello"}, {"utf8": ".", "pAppend": 1}]
        assert _segs_to_text(segs) == "Hello."

    def test_aAppend_and_pAppend_chain(self):
        segs = [
            {"utf8": "Thank"},
            {"utf8": " you"},
            {"utf8": " for"},
            {"utf8": " watch"},
            {"utf8": "ing", "aAppend": 1},
            {"utf8": ".", "pAppend": 1},
        ]
        assert _segs_to_text(segs) == "Thank you for watching."

    def test_empty_utf8_skipped(self):
        segs = [{"utf8": "Hello"}, {"utf8": ""}, {"utf8": "world"}]
        assert _segs_to_text(segs) == "Hello world"

    def test_missing_utf8_skipped(self):
        segs = [{"utf8": "Hello"}, {"tOffsetMs": 500}, {"utf8": " world"}]
        assert _segs_to_text(segs) == "Hello world"

    def test_only_whitespace_stripped(self):
        segs = [{"utf8": "  hello  "}]
        assert _segs_to_text(segs) == "hello"

    def test_newline_only_returns_empty(self):
        segs = [{"utf8": "\n"}]
        assert _segs_to_text(segs) == ""

    def test_empty_segs_returns_empty(self):
        assert _segs_to_text([]) == ""


# ---------------------------------------------------------------------------
# Manual track — straight concat, no dedup
# ---------------------------------------------------------------------------

class TestManualTrack:
    """Manual captions: each event becomes one Segment; no dedup."""

    def _events(self):
        """Load the manual_track.json fixture events."""
        data = json.loads((FIXTURES / "manual_track.json").read_text())
        return data["events"]

    def test_manual_fixture_segment_count(self):
        """Manual track produces exactly 3 segments (4 events, 1 formatting-only)."""
        events = self._events()
        segments = parse_json3(events, kind="manual")
        assert len(segments) == 3

    def test_manual_fixture_reference_output(self):
        """Output matches the pre-verified reference in the fixture file."""
        data = json.loads((FIXTURES / "manual_track.json").read_text())
        expected = data["_reference_output"]
        segments = parse_json3(data["events"], kind="manual")
        assert [s.text for s in segments] == expected

    def test_manual_types(self):
        events = self._events()
        segments = parse_json3(events, kind="manual")
        for seg in segments:
            assert isinstance(seg, Segment)
            assert isinstance(seg.start, float)
            assert isinstance(seg.duration, float)
            assert isinstance(seg.text, str)

    def test_manual_timing(self):
        events = self._events()
        segments = parse_json3(events, kind="manual")
        # First real event: tStartMs=0, dDurationMs=3000
        assert segments[0].start == pytest.approx(0.0)
        assert segments[0].duration == pytest.approx(3.0)
        # Third event: tStartMs=10000
        assert segments[2].start == pytest.approx(10.0)

    def test_manual_aAppend_pAppend_in_fixture(self):
        """Fixture uses aAppend/pAppend; verify text is assembled correctly."""
        data = json.loads((FIXTURES / "manual_track.json").read_text())
        segments = parse_json3(data["events"], kind="manual")
        # Last event: "watching." (aAppend + pAppend)
        assert segments[-1].text == "Thank you for watching."

    def test_manual_no_dedup(self):
        """Overlapping time-windows in a manual track are NOT deduplicated."""
        events = [
            {"tStartMs": 0, "dDurationMs": 5000, "segs": [{"utf8": "Hello"}]},
            {"tStartMs": 2000, "dDurationMs": 5000, "segs": [{"utf8": "world"}]},
        ]
        segments = parse_json3(events, kind="manual")
        assert len(segments) == 2

    def test_manual_empty_events_filtered(self):
        events = [
            {},
            {"tStartMs": 0, "dDurationMs": 1000, "segs": [{"utf8": "Hi"}]},
            {"tStartMs": 1000, "dDurationMs": 1000, "segs": [{"utf8": "\n"}]},
        ]
        segments = parse_json3(events, kind="manual")
        assert len(segments) == 1
        assert segments[0].text == "Hi"

    def test_manual_kind_keyword_args(self):
        """kind='caption' or any non-asr value uses straight concat."""
        events = [
            {"tStartMs": 0, "dDurationMs": 2000, "segs": [{"utf8": "Hello world"}]},
        ]
        assert parse_json3(events, kind="caption") == parse_json3(events, kind="manual")


# ---------------------------------------------------------------------------
# Rolling ASR track — primary dedup + prefix check
# ---------------------------------------------------------------------------

class TestRollingAsr:
    """ASR rolling dedup: the #1 silent-bug gate."""

    def _fixture(self):
        return json.loads((FIXTURES / "rolling_asr.json").read_text())

    def test_asr_fixture_reference_output(self):
        """Output matches the pre-verified reference string — no doubling."""
        data = self._fixture()
        segments = parse_json3(data["events"], kind="asr")
        assert [s.text for s in segments] == data["_reference_output"]

    def test_asr_no_doubling(self):
        """No word should appear more times in the output than in the reference."""
        data = self._fixture()
        segments = parse_json3(data["events"], kind="asr")
        output_words = " ".join(s.text for s in segments).split()
        reference_words = data["_reference_joined"].split()
        assert output_words == reference_words

    def test_asr_naive_join_would_double(self):
        """Confirm that naive join of all events IS longer than dedup output.

        This proves the fixture actually exercises the dedup logic.
        """
        data = self._fixture()
        events = data["events"]
        # Naive: join all non-empty event texts in original order
        naive_texts = []
        for ev in events:
            segs = ev.get("segs") or []
            text = "".join(s.get("utf8", "") for s in segs).strip()
            if text:
                naive_texts.append(text)
        naive = " ".join(naive_texts)

        segments = parse_json3(events, kind="asr")
        deduped = " ".join(s.text for s in segments)

        # Naive is strictly longer (more tokens) due to re-emitted words
        assert len(naive.split()) > len(deduped.split()), (
            f"Naive join ({len(naive.split())} words) should exceed dedup "
            f"({len(deduped.split())} words) — fixture may not show rolling"
        )

    def test_asr_fixture_segment_count(self):
        data = self._fixture()
        segments = parse_json3(data["events"], kind="asr")
        assert len(segments) == 3

    def test_asr_segment_types(self):
        data = self._fixture()
        segments = parse_json3(data["events"], kind="asr")
        for seg in segments:
            assert isinstance(seg, Segment)

    def test_asr_default_kind(self):
        """Default kind='asr' is the dedup path."""
        events = [
            {"tStartMs": 0, "dDurationMs": 3000, "segs": [{"utf8": "Hello world"}]},
            {"tStartMs": 500, "dDurationMs": 2500, "segs": [{"utf8": "world"}]},
        ]
        without_kind = parse_json3(events)
        with_kind = parse_json3(events, kind="asr")
        assert without_kind == with_kind


# ---------------------------------------------------------------------------
# Primary dedup (window coverage)
# ---------------------------------------------------------------------------

class TestPrimaryDedup:
    """Verify the tStartMs >= last_end_ms gate independently."""

    def test_single_event_emitted(self):
        events = [{"tStartMs": 0, "dDurationMs": 5000, "segs": [{"utf8": "Hello"}]}]
        segs = parse_json3(events, kind="asr")
        assert len(segs) == 1
        assert segs[0].text == "Hello"

    def test_non_overlapping_both_emitted(self):
        events = [
            {"tStartMs": 0, "dDurationMs": 3000, "segs": [{"utf8": "Hello"}]},
            {"tStartMs": 3000, "dDurationMs": 3000, "segs": [{"utf8": "world"}]},
        ]
        segs = parse_json3(events, kind="asr")
        assert len(segs) == 2
        assert segs[0].text == "Hello"
        assert segs[1].text == "world"

    def test_overlapping_second_skipped(self):
        events = [
            {"tStartMs": 0, "dDurationMs": 5000, "segs": [{"utf8": "Hello world"}]},
            {"tStartMs": 2000, "dDurationMs": 3000, "segs": [{"utf8": "SHOULD NOT APPEAR"}]},
        ]
        segs = parse_json3(events, kind="asr")
        assert len(segs) == 1
        assert segs[0].text == "Hello world"

    def test_first_event_in_group_carries_full_text(self):
        """First non-overlapping event in a rolling group has the complete phrase."""
        events = [
            # Group 1: first event has all 3 words; later events roll forward
            {"tStartMs": 0,    "dDurationMs": 4000, "segs": [{"utf8": "one two three"}]},
            {"tStartMs": 1000, "dDurationMs": 3000, "segs": [{"utf8": "two three"}]},
            {"tStartMs": 2000, "dDurationMs": 2000, "segs": [{"utf8": "three"}]},
            # Group 2
            {"tStartMs": 4000, "dDurationMs": 3000, "segs": [{"utf8": "four five"}]},
            {"tStartMs": 5000, "dDurationMs": 2000, "segs": [{"utf8": "five"}]},
        ]
        segs = parse_json3(events, kind="asr")
        assert len(segs) == 2
        assert segs[0].text == "one two three"
        assert segs[1].text == "four five"

    def test_gap_between_groups_ok(self):
        """A gap between groups still produces both segments."""
        events = [
            {"tStartMs": 0,    "dDurationMs": 2000, "segs": [{"utf8": "Hello"}]},
            {"tStartMs": 5000, "dDurationMs": 2000, "segs": [{"utf8": "world"}]},
        ]
        segs = parse_json3(events, kind="asr")
        assert len(segs) == 2

    def test_events_sorted_by_start(self):
        """Events out of order in the JSON are sorted before dedup."""
        events = [
            {"tStartMs": 3000, "dDurationMs": 2000, "segs": [{"utf8": "world"}]},
            {"tStartMs": 0,    "dDurationMs": 2000, "segs": [{"utf8": "Hello"}]},
            {"tStartMs": 5000, "dDurationMs": 2000, "segs": [{"utf8": "foo"}]},
        ]
        segs = parse_json3(events, kind="asr")
        assert segs[0].text == "Hello"
        assert segs[1].text == "world"
        assert segs[2].text == "foo"

    def test_empty_text_events_skipped(self):
        events = [
            {},
            {"tStartMs": 0, "dDurationMs": 1000, "segs": []},
            {"tStartMs": 0, "dDurationMs": 1000, "segs": [{"utf8": "\n"}]},
            {"tStartMs": 0, "dDurationMs": 5000, "segs": [{"utf8": "Hello"}]},
        ]
        segs = parse_json3(events, kind="asr")
        assert len(segs) == 1
        assert segs[0].text == "Hello"


# ---------------------------------------------------------------------------
# Prefix check (secondary dedup)
# ---------------------------------------------------------------------------

class TestPrefixCheck:
    """Verify the prefix-check secondary dedup fires when appropriate."""

    def test_prefix_removed(self):
        """If segment A text is a strict prefix of segment B text, A is discarded."""
        events = [
            # Group 1: emits "Hello" (full first event)
            {"tStartMs": 0,    "dDurationMs": 3000, "segs": [{"utf8": "Hello"}]},
            # Group 2: starts exactly where group 1 ends; text extends group 1's text
            {"tStartMs": 3000, "dDurationMs": 3000, "segs": [{"utf8": "Hello world"}]},
        ]
        segs = parse_json3(events, kind="asr")
        # "Hello" is a strict prefix of "Hello world" → discard "Hello"
        assert len(segs) == 1
        assert segs[0].text == "Hello world"

    def test_prefix_check_case_sensitive(self):
        """Prefix check is case-sensitive — 'hello' is NOT a prefix of 'Hello world'."""
        events = [
            {"tStartMs": 0,    "dDurationMs": 3000, "segs": [{"utf8": "hello"}]},
            {"tStartMs": 3000, "dDurationMs": 3000, "segs": [{"utf8": "Hello world"}]},
        ]
        segs = parse_json3(events, kind="asr")
        # "hello" != "Hello world"[0:5] → NOT a prefix → both kept
        assert len(segs) == 2

    def test_equal_text_not_discarded(self):
        """Strict prefix: equal text is NOT a prefix (must be strictly shorter)."""
        events = [
            {"tStartMs": 0,    "dDurationMs": 3000, "segs": [{"utf8": "Hello"}]},
            {"tStartMs": 3000, "dDurationMs": 3000, "segs": [{"utf8": "Hello"}]},
        ]
        segs = parse_json3(events, kind="asr")
        # "Hello" == "Hello" → NOT strict prefix → both kept
        assert len(segs) == 2

    def test_last_segment_never_discarded(self):
        """The last segment has no 'next' to compare against."""
        events = [
            {"tStartMs": 0,    "dDurationMs": 3000, "segs": [{"utf8": "Hello world"}]},
        ]
        segs = parse_json3(events, kind="asr")
        assert len(segs) == 1

    def test_prefix_check_uses_stripped_text(self):
        """Prefix comparison uses whitespace-stripped text."""
        events = [
            # " Hello " stripped to "Hello" which IS a prefix of "Hello world"
            {"tStartMs": 0,    "dDurationMs": 3000, "segs": [{"utf8": " Hello "}]},
            {"tStartMs": 3000, "dDurationMs": 3000, "segs": [{"utf8": "Hello world"}]},
        ]
        segs = parse_json3(events, kind="asr")
        # "Hello" is a prefix of "Hello world" → first segment discarded
        assert len(segs) == 1
        assert segs[0].text == "Hello world"

    def test_non_prefix_pair_both_kept(self):
        events = [
            {"tStartMs": 0,    "dDurationMs": 3000, "segs": [{"utf8": "Hello world"}]},
            {"tStartMs": 3000, "dDurationMs": 3000, "segs": [{"utf8": "foo bar"}]},
        ]
        segs = parse_json3(events, kind="asr")
        assert len(segs) == 2

    def test_prefix_chain_only_first_removed(self):
        """Prefix check is pairwise: only i→i+1 comparison; chains may keep middle."""
        events = [
            {"tStartMs": 0,    "dDurationMs": 3000, "segs": [{"utf8": "Hi"}]},
            {"tStartMs": 3000, "dDurationMs": 3000, "segs": [{"utf8": "Hi there"}]},
            {"tStartMs": 6000, "dDurationMs": 3000, "segs": [{"utf8": "foo"}]},
        ]
        segs = parse_json3(events, kind="asr")
        # "Hi" is prefix of "Hi there" → discard "Hi"
        # "Hi there" is NOT prefix of "foo" → keep "Hi there"
        assert [s.text for s in segs] == ["Hi there", "foo"]


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_empty_events_list(self):
        assert parse_json3([], kind="asr") == []
        assert parse_json3([], kind="manual") == []

    def test_all_formatting_events(self):
        events = [
            {"tStartMs": 0, "dDurationMs": 1000, "segs": [{"utf8": "\n"}]},
            {"tStartMs": 1000, "dDurationMs": 1000, "segs": [{"utf8": "  "}]},
            {},
        ]
        assert parse_json3(events, kind="asr") == []

    def test_missing_tStartMs_defaults_zero(self):
        events = [{"dDurationMs": 2000, "segs": [{"utf8": "Hello"}]}]
        segs = parse_json3(events, kind="asr")
        assert len(segs) == 1
        assert segs[0].start == pytest.approx(0.0)

    def test_missing_dDurationMs_defaults_zero(self):
        events = [
            {"tStartMs": 0, "segs": [{"utf8": "Hello"}]},
            {"tStartMs": 0, "segs": [{"utf8": "should be skipped — tStart=0 < last_end=0+0=0?"}]},
        ]
        # first event: tStart=0 >= last_end=0 → EMIT, last_end=0+0=0
        # second event: tStart=0 >= last_end=0 → EMIT (boundary case: equal)
        segs = parse_json3(events, kind="asr")
        assert len(segs) == 2  # both emitted because 0 >= 0

    def test_timing_conversion_ms_to_sec(self):
        events = [{"tStartMs": 1500, "dDurationMs": 2500, "segs": [{"utf8": "Hello"}]}]
        segs = parse_json3(events, kind="asr")
        assert segs[0].start == pytest.approx(1.5)
        assert segs[0].duration == pytest.approx(2.5)

    def test_returns_segment_objects(self):
        events = [{"tStartMs": 0, "dDurationMs": 1000, "segs": [{"utf8": "Hi"}]}]
        for kind in ("asr", "manual"):
            segs = parse_json3(events, kind=kind)
            assert len(segs) == 1
            assert isinstance(segs[0], Segment)
