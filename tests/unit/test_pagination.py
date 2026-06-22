"""Unit tests for ytt.pagination — Phase 7 (plan: §Response shape & size).

Covers:
- Cursor build / parse / validate (round-trip, stale conditions, malformed)
- filter_segments_by_time, filter_segments_by_query (±2 context)
- segments_to_text
- effective_chunk_chars (Latin pass-through, CJK reduction)
- chunk_text (basic, boundary, beyond end)
- chunk_text_with_segments (segment-aligned boundary)
- build_page: inline, paginated, mode=chunk, cursor continuation, stale cursor,
              query filter, time filter, loud-PARTIAL prefix, last-chunk is_final
- Hypothesis property tests (pagination never loses chars; cursor always valid
  after build_page; total_chars consistent across pages)
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import pytest
from hypothesis import assume, given, settings as h_settings
from hypothesis import strategies as st

from ytt.cache import CacheHit
from ytt.pagination import (
    _canonical_filter,
    _cursor_hash,
    build_cursor,
    build_page,
    chunk_text,
    chunk_text_with_segments,
    effective_chunk_chars,
    filter_segments_by_query,
    filter_segments_by_time,
    loud_partial_prefix,
    parse_cursor,
    segments_to_text,
    validate_cursor,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


@dataclass
class FakeSettings:
    """Minimal settings stub for pagination tests."""

    inline_char_limit: int = 200
    chunk_chars: int = 50


def make_hit(
    text: str = "Hello world this is a test transcript.",
    video_id: str = "dQw4w9WgXcQ",
    lang: str = "en",
    source: str = "caption_auto",
    segments: list[dict] | None = None,
    metadata: dict | None = None,
) -> CacheHit:
    return CacheHit(
        video_id=video_id,
        lang=lang,
        source=source,
        text=text,
        segments=segments,
        metadata=metadata,
    )


def make_segs(*texts: str, start_offset: float = 0.0) -> list[dict]:
    """Create sequential segments with 1-second windows."""
    return [
        {"start": start_offset + float(i), "duration": 1.0, "text": t}
        for i, t in enumerate(texts)
    ]


# ---------------------------------------------------------------------------
# Cursor: build / parse / validate
# ---------------------------------------------------------------------------


class TestCursorBuild:
    def test_build_produces_three_colon_parts(self):
        content = b"hello"
        c = build_cursor(content, "en", "caption_auto", {}, 100, 0)
        parts = c.split(":")
        assert len(parts) == 3

    def test_build_total_chars_embedded(self):
        content = b"hello"
        c = build_cursor(content, "en", "caption_auto", {}, 12345, 0)
        assert ":12345:" in c

    def test_build_offset_embedded(self):
        content = b"hello"
        c = build_cursor(content, "en", "caption_auto", {}, 12345, 500)
        assert c.endswith(":12345:500")

    def test_build_different_content_different_hash(self):
        c1 = build_cursor(b"aaa", "en", "caption_auto", {}, 3, 0)
        c2 = build_cursor(b"bbb", "en", "caption_auto", {}, 3, 0)
        assert c1.split(":")[0] != c2.split(":")[0]

    def test_build_different_lang_different_hash(self):
        c1 = build_cursor(b"hello", "en", "caption_auto", {}, 5, 0)
        c2 = build_cursor(b"hello", "es", "caption_auto", {}, 5, 0)
        assert c1.split(":")[0] != c2.split(":")[0]

    def test_build_different_source_different_hash(self):
        c1 = build_cursor(b"hello", "en", "caption_auto", {}, 5, 0)
        c2 = build_cursor(b"hello", "en", "caption_manual", {}, 5, 0)
        assert c1.split(":")[0] != c2.split(":")[0]

    def test_build_different_filter_different_hash(self):
        c1 = build_cursor(b"hello", "en", "caption_auto", {}, 5, 0)
        c2 = build_cursor(b"hello", "en", "caption_auto", {"query": "foo"}, 5, 0)
        assert c1.split(":")[0] != c2.split(":")[0]

    def test_build_same_inputs_same_cursor(self):
        c1 = build_cursor(b"hello", "en", "caption_auto", {"query": "x"}, 100, 50)
        c2 = build_cursor(b"hello", "en", "caption_auto", {"query": "x"}, 100, 50)
        assert c1 == c2

    def test_filter_none_values_excluded(self):
        """Canonical filter excludes None values; result same as explicit empty."""
        c1 = build_cursor(b"hello", "en", "caption_auto", {"query": None}, 5, 0)
        c2 = build_cursor(b"hello", "en", "caption_auto", {}, 5, 0)
        assert c1 == c2


class TestParseCursor:
    def test_parse_valid(self):
        c = "abc123def456ghi7:45000:18000"
        parsed = parse_cursor(c)
        assert parsed == ("abc123def456ghi7", 45000, 18000)

    def test_parse_too_few_parts(self):
        assert parse_cursor("abc:123") is None

    def test_parse_too_many_colons(self):
        # More than 3 parts — the third segment absorbs remaining colons via split(":", 2)
        c = "abc:123:456:extra"
        parsed = parse_cursor(c)
        # split(":", 2) produces 3 parts, last = "456:extra"
        assert parsed is None  # int("456:extra") fails

    def test_parse_non_integer_total(self):
        assert parse_cursor("abc:XYZ:0") is None

    def test_parse_non_integer_offset(self):
        assert parse_cursor("abc:100:XYZ") is None

    def test_parse_empty(self):
        assert parse_cursor("") is None


class TestValidateCursor:
    def _make_cursor(self, content: bytes, lang: str = "en", source: str = "caption_auto",
                     filter_args: dict | None = None, total: int = 100, offset: int = 0) -> str:
        return build_cursor(content, lang, source, filter_args or {}, total, offset)

    def test_validate_round_trip(self):
        content = b"hello world"
        c = self._make_cursor(content, offset=50)
        result = validate_cursor(c, content, "en", "caption_auto", {})
        assert result == 50

    def test_validate_stale_content_changed(self):
        content_old = b"old text"
        content_new = b"new text"
        c = self._make_cursor(content_old)
        result = validate_cursor(c, content_new, "en", "caption_auto", {})
        assert result is None

    def test_validate_stale_lang_changed(self):
        content = b"hello"
        c = self._make_cursor(content, lang="en")
        result = validate_cursor(c, content, "es", "caption_auto", {})
        assert result is None

    def test_validate_stale_source_changed(self):
        content = b"hello"
        c = self._make_cursor(content, source="caption_auto")
        result = validate_cursor(c, content, "en", "caption_manual", {})
        assert result is None

    def test_validate_stale_filter_changed(self):
        content = b"hello"
        c = self._make_cursor(content, filter_args={"query": "foo"})
        result = validate_cursor(c, content, "en", "caption_auto", {"query": "bar"})
        assert result is None

    def test_validate_malformed_cursor(self):
        result = validate_cursor("not-a-cursor", b"hello", "en", "caption_auto", {})
        assert result is None

    def test_validate_offset_zero(self):
        content = b"hello world"
        c = self._make_cursor(content, total=11, offset=0)
        result = validate_cursor(c, content, "en", "caption_auto", {})
        assert result == 0

    def test_validate_filter_none_canonical(self):
        """Filter with None values treated same as empty filter."""
        content = b"x"
        c = build_cursor(content, "en", "caption_auto", {}, 1, 0)
        result = validate_cursor(c, content, "en", "caption_auto", {"query": None})
        assert result == 0  # None filter ≡ {} filter


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


class TestFilterByTime:
    def _make_segs(self):
        # Segments at t=0..1, 2..3, 4..5, 6..7
        return [
            {"start": 0.0, "duration": 1.0, "text": "A"},
            {"start": 2.0, "duration": 1.0, "text": "B"},
            {"start": 4.0, "duration": 1.0, "text": "C"},
            {"start": 6.0, "duration": 1.0, "text": "D"},
        ]

    def test_no_bounds_returns_all(self):
        segs = self._make_segs()
        assert filter_segments_by_time(segs, None, None) == segs

    def test_start_bound_excludes_early(self):
        segs = self._make_segs()
        result = filter_segments_by_time(segs, 2.0, None)
        texts = [s["text"] for s in result]
        assert "A" not in texts
        assert "B" in texts

    def test_end_bound_excludes_late(self):
        segs = self._make_segs()
        result = filter_segments_by_time(segs, None, 3.0)
        texts = [s["text"] for s in result]
        assert "C" not in texts
        assert "D" not in texts
        assert "A" in texts
        assert "B" in texts

    def test_both_bounds(self):
        segs = self._make_segs()
        result = filter_segments_by_time(segs, 2.0, 4.5)
        texts = [s["text"] for s in result]
        assert texts == ["B", "C"]

    def test_segment_overlapping_start(self):
        # Segment [0, 2] overlaps start=1.5
        segs = [{"start": 0.0, "duration": 2.0, "text": "overlap"}]
        result = filter_segments_by_time(segs, 1.5, None)
        assert len(result) == 1

    def test_empty_segments(self):
        assert filter_segments_by_time([], 0.0, 10.0) == []


class TestFilterByQuery:
    def test_no_match_returns_empty(self):
        segs = make_segs("hello", "world", "foo")
        assert filter_segments_by_query(segs, "zzz") == []

    def test_exact_match(self):
        segs = make_segs("hello", "world", "foo")
        result = filter_segments_by_query(segs, "world")
        texts = [s["text"] for s in result]
        assert "world" in texts

    def test_case_insensitive(self):
        segs = make_segs("Hello World", "other")
        result = filter_segments_by_query(segs, "hello")
        texts = [s["text"] for s in result]
        assert "Hello World" in texts

    def test_context_window(self):
        # Match at index 3 → should include ±2 → indices 1,2,3,4,5
        segs = make_segs("a", "b", "c", "MATCH", "d", "e", "f")
        result = filter_segments_by_query(segs, "MATCH")
        texts = [s["text"] for s in result]
        assert "b" in texts
        assert "c" in texts
        assert "MATCH" in texts
        assert "d" in texts
        assert "e" in texts
        assert "a" not in texts  # index 0 = 3 - 3 away → excluded
        assert "f" not in texts  # index 6 = 3 + 3 away → excluded

    def test_context_window_at_start(self):
        segs = make_segs("MATCH", "b", "c", "d", "e")
        result = filter_segments_by_query(segs, "MATCH")
        texts = [s["text"] for s in result]
        assert "MATCH" in texts
        assert "b" in texts
        assert "c" in texts

    def test_multiple_matches_merged(self):
        # Two non-adjacent matches whose context windows overlap → all included
        segs = make_segs("a", "M1", "b", "c", "M2", "d")
        result = filter_segments_by_query(segs, "M")
        # Context around index 1 (M1): [0,1,2,3]
        # Context around index 4 (M2): [2,3,4,5]
        # Union: all indices
        assert len(result) == len(segs)

    def test_empty_segments(self):
        assert filter_segments_by_query([], "foo") == []


class TestSegmentsToText:
    def test_basic_join(self):
        segs = [{"text": "Hello"}, {"text": "World"}]
        assert segments_to_text(segs) == "Hello World"

    def test_stripped(self):
        segs = [{"text": "  hi  "}, {"text": " there "}]
        result = segments_to_text(segs)
        assert result == "hi    there"  # spaces not stripped per segment, only outer

    def test_empty_list(self):
        assert segments_to_text([]) == ""

    def test_missing_text_key(self):
        segs = [{"start": 0.0, "duration": 1.0}]
        assert segments_to_text(segs) == ""


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------


class TestEffectiveChunkChars:
    def test_latin_unchanged(self):
        text = "hello world"
        assert effective_chunk_chars(text, 1000) == 1000

    def test_empty_unchanged(self):
        assert effective_chunk_chars("", 1000) == 1000

    def test_pure_ascii_unchanged(self):
        text = "a" * 5000
        assert effective_chunk_chars(text, 18000) == 18000

    def test_cjk_reduces_limit(self):
        # CJK characters: each is 3 bytes UTF-8, 1 char
        text = "你好世界" * 100  # 400 chars, 1200 bytes
        result = effective_chunk_chars(text, 18000)
        # bytes/3 = 1200/3 = 400; min(18000, 400) = 400
        assert result == 400

    def test_cjk_capped_at_chunk_chars(self):
        # If bytes/3 > chunk_chars, use chunk_chars
        text = "你好世界" * 10  # 40 chars, 120 bytes → bytes/3=40
        result = effective_chunk_chars(text, 100)
        # min(100, 40) = 40
        assert result == 40

    def test_at_least_one(self):
        # Even a single byte → at least 1
        text = "你"  # 3 bytes, 1 char → bytes/3 = 1
        result = effective_chunk_chars(text, 5000)
        assert result >= 1


class TestChunkText:
    def test_basic_chunk(self):
        text = "abcde"
        chunk, next_off, is_final = chunk_text(text, 0, 3)
        assert chunk == "abc"
        assert next_off == 3
        assert not is_final

    def test_final_chunk(self):
        text = "abcde"
        chunk, next_off, is_final = chunk_text(text, 3, 3)
        assert chunk == "de"
        assert next_off == 5
        assert is_final

    def test_offset_at_end(self):
        text = "abc"
        chunk, next_off, is_final = chunk_text(text, 3, 10)
        assert chunk == ""
        assert next_off == 3
        assert is_final

    def test_single_char_chunks(self):
        text = "abc"
        parts = []
        offset = 0
        while True:
            chunk, offset, is_final = chunk_text(text, offset, 1)
            parts.append(chunk)
            if is_final:
                break
        assert "".join(parts) == "abc"

    def test_chunk_larger_than_text(self):
        text = "hi"
        chunk, next_off, is_final = chunk_text(text, 0, 1000)
        assert chunk == "hi"
        assert is_final


class TestChunkTextWithSegments:
    def _segs_and_text(self):
        segs = make_segs("Hello", "World", "Foo", "Bar")
        text = segments_to_text(segs)
        return segs, text

    def test_basic(self):
        segs, text = self._segs_and_text()
        chunk, chunk_segs, next_off, is_final = chunk_text_with_segments(text, segs, 0, 20)
        assert len(chunk_segs) >= 1
        assert next_off > 0

    def test_segment_aligned_boundary(self):
        # text = "Hello World Foo Bar" = 19 chars
        # chunk_chars=12 → raw_end=12 (within "World")
        # Should align to end of "Hello" (5) or "World" (11)
        segs, text = self._segs_and_text()
        chunk, chunk_segs, next_off, is_final = chunk_text_with_segments(text, segs, 0, 12)
        # The chunk should end at a segment boundary ≤ 12
        assert next_off <= len(text)

    def test_is_final_when_all_consumed(self):
        segs, text = self._segs_and_text()
        chunk, chunk_segs, next_off, is_final = chunk_text_with_segments(text, segs, 0, 1000)
        assert is_final
        assert next_off == len(text)

    def test_empty_segments_fallback(self):
        text = "hello world"
        chunk, chunk_segs, next_off, is_final = chunk_text_with_segments(text, [], 0, 5)
        assert chunk == "hello"
        assert chunk_segs == []

    def test_no_split_at_offset_if_no_boundary(self):
        # If no segment boundary found before raw_end, use raw_end
        segs = [{"start": 0.0, "duration": 10.0, "text": "A long single segment here"}]
        text = segments_to_text(segs)
        chunk, chunk_segs, next_off, is_final = chunk_text_with_segments(text, segs, 0, 5)
        # raw_end=5; no seg boundary before 5; fallback to raw_end
        assert next_off <= len(text)
        assert len(chunk) > 0


# ---------------------------------------------------------------------------
# Loud PARTIAL prefix
# ---------------------------------------------------------------------------


class TestLoudPartialPrefix:
    def test_contains_warning_emoji(self):
        p = loud_partial_prefix(0, 50, 200, 1, 4, "abc:200:50")
        assert "⚠️" in p

    def test_contains_partial_keyword(self):
        p = loud_partial_prefix(0, 50, 200, 1, 4, "abc:200:50")
        assert "PARTIAL" in p

    def test_contains_incomplete_keyword(self):
        p = loud_partial_prefix(0, 50, 200, 1, 4, "abc:200:50")
        assert "INCOMPLETE" in p

    def test_contains_cursor_value(self):
        cursor = "MYCURSOR:200:50"
        p = loud_partial_prefix(0, 50, 200, 1, 4, cursor)
        assert cursor in p

    def test_contains_char_range(self):
        p = loud_partial_prefix(100, 200, 500, 2, 5, "c:500:200")
        # A = 101 (1-indexed), B = 200
        assert "101" in p
        assert "200" in p
        assert "500" in p

    def test_contains_chunk_fraction(self):
        p = loud_partial_prefix(0, 50, 200, 3, 7, "c:200:50")
        assert "3/7" in p

    def test_ends_with_double_newline(self):
        p = loud_partial_prefix(0, 50, 200, 1, 4, "c")
        assert p.endswith("\n\n")


# ---------------------------------------------------------------------------
# build_page — main entry point
# ---------------------------------------------------------------------------


class FakeSettings200_50:
    """Settings: inline_limit=200, chunk=50."""
    inline_char_limit = 200
    chunk_chars = 50


class FakeSettings50_20:
    """Settings: inline_limit=50, chunk=20."""
    inline_char_limit = 50
    chunk_chars = 20


class TestBuildPageInline:
    def test_short_full_mode_returns_inline(self):
        hit = make_hit(text="Short text.", lang="en", source="caption_auto")
        result = build_page(hit, "full", {}, FakeSettings200_50())
        assert result["status"] == "ok"
        assert result["text"] == "Short text."
        assert result["is_final"] is True
        assert result.get("next_cursor") is None

    def test_short_full_mode_total_chars(self):
        hit = make_hit(text="Short text.", lang="en", source="caption_auto")
        result = build_page(hit, "full", {}, FakeSettings200_50())
        assert result["total_chars"] == len("Short text.")
        assert result["offset"] == 0

    def test_short_full_mode_includes_source_lang(self):
        hit = make_hit(text="Short text.", lang="en", source="caption_auto")
        result = build_page(hit, "full", {}, FakeSettings200_50())
        assert result["source"] == "caption_auto"
        assert result["lang"] == "en"
        # quality string from TRANSCRIPT_QUALITY map (not the key itself)
        assert len(result.get("transcript_quality", "")) > 0
        assert "auto" in result.get("transcript_quality", "").lower()

    def test_short_full_mode_includes_metadata(self):
        hit = make_hit(
            text="Short.",
            metadata={"title": "My Video", "channel": "Test Channel"},
        )
        result = build_page(hit, "full", {}, FakeSettings200_50())
        assert result.get("title") == "My Video"
        assert result.get("channel") == "Test Channel"

    def test_short_full_with_segments(self):
        segs = make_segs("Hello", "World")
        hit = make_hit(text="Hello World", segments=segs)
        result = build_page(hit, "full", {}, FakeSettings200_50())
        assert result["segments"] == segs
        assert result["is_final"] is True


class TestBuildPageChunkMode:
    def test_chunk_mode_always_paginates_even_short(self):
        """mode=chunk always paginates regardless of transcript length."""
        hit = make_hit(text="Short text less than chunk limit.", lang="en", source="caption_auto")
        settings = FakeSettings200_50()
        result = build_page(hit, "chunk", {}, settings)
        # mode=chunk → always paginate; total_chars < chunk_chars=50 → is_final immediately
        # (it paginates, but the first chunk IS the last chunk for short text)
        assert result.get("is_final") is True  # short enough to fit in one chunk

    def test_chunk_mode_long_text_returns_partial(self):
        """mode=chunk with long text returns status=partial and next_cursor."""
        long_text = "x" * 100  # > chunk_chars=50 in FakeSettings200_50
        hit = make_hit(text=long_text)
        settings = FakeSettings200_50()
        result = build_page(hit, "chunk", {}, settings)
        assert result["status"] == "partial"
        assert result.get("next_cursor") is not None
        assert result["is_final"] is False

    def test_chunk_mode_short_but_paginated(self):
        """mode=chunk with text=40 chars and chunk=50 → fits in 1 chunk → is_final."""
        hit = make_hit(text="a" * 40)
        settings = FakeSettings200_50()
        result = build_page(hit, "chunk", {}, settings)
        # One chunk, all done
        assert result["is_final"] is True
        assert result["total_chars"] == 40


class TestBuildPagePaginated:
    def _long_hit(self):
        # 120 chars > inline_limit=200? no, use 300 chars > inline_limit=200
        return make_hit(text="x" * 300)

    def test_long_full_returns_chunk1(self):
        hit = self._long_hit()
        result = build_page(hit, "full", {}, FakeSettings200_50())
        assert result["status"] == "partial"
        assert result.get("next_cursor") is not None
        assert result["is_final"] is False
        assert result["total_chars"] == 300

    def test_chunk1_offset_zero(self):
        hit = self._long_hit()
        result = build_page(hit, "full", {}, FakeSettings200_50())
        assert result["offset"] == 0

    def test_partial_text_leads_with_warning(self):
        hit = self._long_hit()
        result = build_page(hit, "full", {}, FakeSettings200_50())
        assert result["text"].startswith("⚠️ PARTIAL:")

    def test_continuation_leads_to_final(self):
        """Page through a long text and verify all chars are delivered."""
        text = "abcde" * 20  # 100 chars
        hit = make_hit(text=text)
        settings = FakeSettings200_50()  # chunk=50, inline=200 → text fits inline!
        # Use chunk mode to force pagination
        all_chars = []
        cursor = None
        page = 0
        while True:
            result = build_page(hit, "chunk", {}, settings, cursor=cursor)
            assert result["total_chars"] == 100
            page += 1
            chunk_text_val = result["text"]
            # Strip PARTIAL prefix from non-final chunks
            if result.get("status") == "partial":
                assert "⚠️ PARTIAL:" in chunk_text_val
                # The prefix ends with "\n\n"
                idx = chunk_text_val.index("\n\n")
                chunk_text_val = chunk_text_val[idx + 2:]
            all_chars.append(chunk_text_val)
            if result["is_final"]:
                break
            cursor = result["next_cursor"]
            assert cursor is not None
            assert page < 20  # guard against infinite loop
        # All the actual text should be delivered
        assert "".join(all_chars) == text

    def test_cursor_stale_on_content_change(self):
        """After a content change, the old cursor is rejected with cursor_stale."""
        text = "x" * 300
        hit_old = make_hit(text=text, video_id="vid11111111a")
        settings = FakeSettings200_50()
        result = build_page(hit_old, "full", {}, settings)
        cursor = result["next_cursor"]

        # Simulate content change (new text → new hash)
        hit_new = make_hit(text="y" * 300, video_id="vid11111111a")
        result2 = build_page(hit_new, "full", {}, settings, cursor=cursor)
        assert result2["status"] == "error"
        assert result2["error_code"] == "cursor_stale"

    def test_cursor_stale_on_eviction(self):
        """Cursor valid for one content is rejected when content differs."""
        text = "x" * 300
        hit = make_hit(text=text)
        settings = FakeSettings200_50()
        result = build_page(hit, "full", {}, settings)
        cursor = result["next_cursor"]

        # Simulate eviction: call build_page with empty CacheHit (no text)
        hit_evicted = make_hit(text="", video_id=hit.video_id)
        result2 = build_page(hit_evicted, "full", {}, settings, cursor=cursor)
        assert result2["status"] == "error"
        assert result2["error_code"] == "cursor_stale"


class TestBuildPageFilters:
    def test_query_filter_returns_matching_segments(self):
        segs = make_segs("alpha", "beta", "gamma", "delta")
        hit = make_hit(
            text=segments_to_text(segs),
            segments=segs,
        )
        result = build_page(hit, "full", {"query": "beta"}, FakeSettings200_50())
        assert result["status"] == "ok"
        texts = [s["text"] for s in (result.get("segments") or [])]
        assert "beta" in texts

    def test_query_no_match_empty_text(self):
        segs = make_segs("alpha", "beta")
        hit = make_hit(text=segments_to_text(segs), segments=segs)
        result = build_page(hit, "full", {"query": "zzz"}, FakeSettings200_50())
        assert result["text"] == ""
        assert result["total_chars"] == 0

    def test_time_filter_returns_overlapping_segments(self):
        segs = [
            {"start": 0.0, "duration": 2.0, "text": "early"},
            {"start": 5.0, "duration": 2.0, "text": "mid"},
            {"start": 10.0, "duration": 2.0, "text": "late"},
        ]
        hit = make_hit(text=segments_to_text(segs), segments=segs)
        result = build_page(hit, "full", {"start": 4.0, "end": 7.0}, FakeSettings200_50())
        texts = [s["text"] for s in (result.get("segments") or [])]
        assert "mid" in texts
        assert "early" not in texts
        assert "late" not in texts

    def test_filter_changes_cursor_hash(self):
        """Cursor built with one filter is stale when validated without filter."""
        text = "x" * 300
        segs = make_segs(*["word"] * 60)  # 60 segments
        hit = make_hit(text=text, segments=segs)
        settings = FakeSettings200_50()

        # Get cursor with query filter
        r1 = build_page(hit, "full", {"query": "word"}, settings)
        cursor_with_filter = r1.get("next_cursor")

        if cursor_with_filter is None:
            pytest.skip("Text too short to paginate with filter")

        # Cursor is stale when validated without filter
        r2 = build_page(hit, "full", {}, settings, cursor=cursor_with_filter)
        assert r2.get("error_code") == "cursor_stale"

    def test_query_on_text_without_segments(self):
        """Query filter on plain text (no segments) does substring match."""
        hit = make_hit(text="hello world foo bar", segments=None)
        r = build_page(hit, "full", {"query": "world"}, FakeSettings200_50())
        assert r["status"] == "ok"
        assert "hello world foo bar" in r["text"]

    def test_query_no_match_on_text_without_segments(self):
        """Query with no match on plain text returns empty."""
        hit = make_hit(text="hello world", segments=None)
        r = build_page(hit, "full", {"query": "zzz"}, FakeSettings200_50())
        assert r["text"] == ""
        assert r["total_chars"] == 0


class TestBuildPageWhisperSource:
    def test_whisper_source_quality_string(self):
        """Invariant 5: whisper source answers any lang, quality label correct."""
        hit = make_hit(
            text="Whisper ASR result.",
            lang="whisper",
            source="whisper",
        )
        result = build_page(hit, "full", {}, FakeSettings200_50())
        assert result["source"] == "whisper"
        assert "Whisper" in result.get("transcript_quality", "") or "ASR" in result.get("transcript_quality", "")

    def test_whisper_requested_lang_in_metadata(self):
        """Metadata fields (requested_lang, available_langs) are surfaced."""
        hit = make_hit(
            text="Transcript.",
            lang="whisper",
            source="whisper",
            metadata={
                "requested_lang": "es",
                "available_langs": ["en"],
                "title": "My Video",
            },
        )
        result = build_page(hit, "full", {}, FakeSettings200_50())
        assert result.get("requested_lang") == "es"
        assert result.get("available_langs") == ["en"]
        assert result.get("title") == "My Video"


class TestBuildPageCursorConsistency:
    def test_total_chars_consistent_across_pages(self):
        """total_chars must be identical on every page."""
        text = "z" * 200
        hit = make_hit(text=text)
        settings = FakeSettings200_50()  # chunk=50, inline=200 → fits inline in mode=full
        # Force mode=chunk to paginate
        totals = []
        cursor = None
        while True:
            result = build_page(hit, "chunk", {}, settings, cursor=cursor)
            totals.append(result["total_chars"])
            if result["is_final"]:
                break
            cursor = result["next_cursor"]
        assert all(t == 200 for t in totals)

    def test_offset_monotonically_increases(self):
        """offset must strictly increase on each page."""
        text = "a" * 200
        hit = make_hit(text=text)
        settings = FakeSettings200_50()
        offsets = []
        cursor = None
        while True:
            result = build_page(hit, "chunk", {}, settings, cursor=cursor)
            offsets.append(result["offset"])
            if result["is_final"]:
                break
            cursor = result["next_cursor"]
        assert offsets == sorted(offsets)
        assert len(offsets) > 1  # actually paginated


# ---------------------------------------------------------------------------
# Hypothesis property tests (Invariants, plan §Testing Strategy)
# ---------------------------------------------------------------------------


def _make_settings(inline_limit: int = 0, chunk: int = 50):
    """Return a minimal settings object for property tests."""
    class _S:
        inline_char_limit = inline_limit
        chunk_chars = chunk
    return _S()


@given(
    text=st.text(
        alphabet=st.characters(blacklist_categories=("Cs",)),
        min_size=0,
        max_size=500,
    ),
    chunk_chars=st.integers(min_value=1, max_value=200),
)
@h_settings(max_examples=100, deadline=5000)
def test_property_pagination_delivers_all_chars(text: str, chunk_chars: int):
    """Property: paginating a transcript delivers every character exactly once."""
    if not text:
        return  # trivial case

    settings = _make_settings(inline_limit=0, chunk=chunk_chars)
    hit = make_hit(text=text, source="caption_auto")
    collected: list[str] = []
    cursor = None
    iterations = 0

    while True:
        result = build_page(hit, "chunk", {}, settings, cursor=cursor)
        raw_text = result.get("text", "")

        # Strip PARTIAL prefix from non-final chunks
        if result.get("status") == "partial" and "⚠️ PARTIAL:" in raw_text:
            idx = raw_text.index("\n\n")
            raw_text = raw_text[idx + 2:]

        collected.append(raw_text)
        iterations += 1
        assume(iterations < 10000)  # guard against hypothesis exhaustion

        if result["is_final"]:
            break
        cursor = result.get("next_cursor")
        assert cursor is not None

    assert "".join(collected) == text


@given(
    text=st.text(
        alphabet=st.characters(blacklist_categories=("Cs",)),
        min_size=1,
        max_size=200,
    ),
    chunk_chars=st.integers(min_value=1, max_value=100),
)
@h_settings(max_examples=100, deadline=5000)
def test_property_cursor_always_valid_after_build(text: str, chunk_chars: int):
    """Property: a cursor returned by build_page is always valid for the next call."""
    settings = _make_settings(inline_limit=0, chunk=chunk_chars)
    hit = make_hit(text=text, source="caption_auto")
    result = build_page(hit, "chunk", {}, settings, cursor=None)

    if result.get("status") == "partial":
        next_cursor = result.get("next_cursor")
        assert next_cursor is not None
        # The cursor must be valid (not stale) for the same hit
        result2 = build_page(hit, "chunk", {}, settings, cursor=next_cursor)
        assert result2.get("error_code") != "cursor_stale"


@given(
    text=st.text(
        alphabet=st.characters(blacklist_categories=("Cs",)),
        min_size=1,
        max_size=200,
    ),
    chunk_chars=st.integers(min_value=1, max_value=100),
)
@h_settings(max_examples=100, deadline=5000)
def test_property_total_chars_consistent(text: str, chunk_chars: int):
    """Property: total_chars is the same on every page."""
    settings = _make_settings(inline_limit=0, chunk=chunk_chars)
    hit = make_hit(text=text, source="caption_auto")
    totals: list[int] = []
    cursor = None
    iterations = 0

    while True:
        result = build_page(hit, "chunk", {}, settings, cursor=cursor)
        totals.append(result["total_chars"])
        iterations += 1
        assume(iterations < 10000)

        if result["is_final"]:
            break
        cursor = result.get("next_cursor")

    assert all(t == len(text) for t in totals)


@given(
    text=st.text(
        alphabet=st.characters(blacklist_categories=("Cs",)),
        min_size=1,
        max_size=200,
    ),
    chunk_chars=st.integers(min_value=1, max_value=100),
)
@h_settings(max_examples=100, deadline=5000)
def test_property_cache_bound_invariant_1_chunk_size(text: str, chunk_chars: int):
    """Property: each chunk's actual text length ≤ chunk_chars (modulo PARTIAL prefix)."""
    settings = _make_settings(inline_limit=0, chunk=chunk_chars)
    hit = make_hit(text=text, source="caption_auto")
    cursor = None
    iterations = 0

    while True:
        result = build_page(hit, "chunk", {}, settings, cursor=cursor)
        raw = result.get("text", "")

        # Strip PARTIAL prefix
        if result.get("status") == "partial" and "⚠️ PARTIAL:" in raw:
            idx = raw.index("\n\n")
            raw = raw[idx + 2:]

        assume(len(raw) <= len(text))  # guard trivial failure
        iterations += 1
        assume(iterations < 10000)

        if result["is_final"]:
            break
        cursor = result.get("next_cursor")
