"""Unit tests for URL -> canonical video_id (plan: load-bearing canonicalizer).

Covers every URL form -> one id, the reject set -> ``bad_url``, and
**Invariant 3** idempotence (``canon(canon(x)) == canon(x)``) both by example
and via a Hypothesis property test over random valid ids.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ytt.canonicalize import canonicalize
from ytt.errors import BAD_URL, YttError

VID = "dQw4w9WgXcQ"

VALID_FORMS = [
    VID,  # bare id
    "ab_cd-EF123",  # id with _ and -
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "http://youtube.com/watch?v=dQw4w9WgXcQ",
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLabc&index=3&t=42s",
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ&pp=ygUK",
    "https://youtu.be/dQw4w9WgXcQ",
    "https://youtu.be/dQw4w9WgXcQ?t=30",
    "https://www.youtube.com/shorts/dQw4w9WgXcQ",
    "https://www.youtube.com/live/dQw4w9WgXcQ",
    "https://www.youtube.com/embed/dQw4w9WgXcQ",
    "https://www.youtube.com/embed/dQw4w9WgXcQ?autoplay=1",
    "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ",
    "https://www.youtube.com/v/dQw4w9WgXcQ",
    "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
    "youtu.be/dQw4w9WgXcQ",  # scheme-less
    "www.youtube.com/watch?v=dQw4w9WgXcQ",  # scheme-less
    "  https://youtu.be/dQw4w9WgXcQ  ",  # surrounding whitespace
]

INVALID_FORMS = [
    "",
    "   ",
    "https://www.youtube.com/playlist?list=PLabc",
    "https://www.youtube.com/channel/UCabcdefghijklmnop",
    "https://www.youtube.com/@SomeHandle",
    "https://www.youtube.com/@SomeHandle/videos",
    "https://www.youtube.com/c/SomeChannel",
    "https://www.youtube.com/user/SomeUser",
    "https://www.youtube.com/results?search_query=cats",
    "https://www.youtube.com/feed/subscriptions",
    "https://www.youtube.com/hashtag/cats",
    "https://www.youtube.com/watch?list=PLabc",  # no v=
    "https://example.com/watch?v=dQw4w9WgXcQ",  # wrong host
    "https://youtu.be/",  # empty id
    "https://www.youtube.com/watch?v=tooSHORT",  # invalid id length
    "https://www.youtube.com/watch?v=way_too_long_id_here",
    "https://www.youtube.com/",  # bare host
]


@pytest.mark.parametrize("form", VALID_FORMS)
def test_valid_forms_map_to_id(form):
    expected = "ab_cd-EF123" if form == "ab_cd-EF123" else VID
    assert canonicalize(form) == expected


@pytest.mark.parametrize("form", VALID_FORMS)
def test_idempotent_on_valid_forms(form):
    once = canonicalize(form)
    assert canonicalize(once) == once  # Invariant 3


@pytest.mark.parametrize("form", INVALID_FORMS)
def test_invalid_forms_raise_bad_url(form):
    with pytest.raises(YttError) as ei:
        canonicalize(form)
    assert ei.value.error_code == BAD_URL


@pytest.mark.parametrize(
    "host_tmpl",
    [
        "https://www.youtube.com/watch?v={vid}",
        "https://youtu.be/{vid}",
        "https://www.youtube.com/shorts/{vid}",
        "https://www.youtube.com/embed/{vid}?x=1",
        "https://music.youtube.com/watch?v={vid}&list=PLz",
        "{vid}",
    ],
)
@given(vid=st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-", min_size=11, max_size=11))
def test_property_canon_idempotent_and_extracts_id(host_tmpl, vid):
    url = host_tmpl.format(vid=vid)
    result = canonicalize(url)
    assert result == vid
    # Invariant 3: canon(canon(x)) == canon(x)
    assert canonicalize(result) == result
