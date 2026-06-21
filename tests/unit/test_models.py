"""Unit tests for the data models (plan: Data Models + field matrix)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ytt.models import (
    TRANSCRIPT_QUALITY,
    EgressReport,
    Segment,
    TranscriptRequest,
    TranscriptResult,
    WhisperJob,
    transcript_quality_for,
)


def test_request_defaults():
    r = TranscriptRequest(url="https://youtu.be/dQw4w9WgXcQ")
    assert r.mode == "full"
    assert r.lang is None and r.cursor is None and r.query is None


def test_request_rejects_unknown_field():
    with pytest.raises(ValidationError):
        TranscriptRequest(url="x", bogus=1)


def test_request_rejects_invalid_mode():
    with pytest.raises(ValidationError):
        TranscriptRequest(url="x", mode="summary")  # dropped in v1


def test_request_query_excludes_time_bounds():
    with pytest.raises(ValidationError):
        TranscriptRequest(url="x", query="cats", start=10.0)
    # query alone, or start/end alone, are fine
    assert TranscriptRequest(url="x", query="cats").query == "cats"
    assert TranscriptRequest(url="x", start=10.0, end=20.0).start == 10.0


def test_result_minimal():
    res = TranscriptResult(video_id="dQw4w9WgXcQ", status="ok")
    assert res.transcript_url is None  # reserved/unset v1
    assert res.text is None


def test_result_rejects_bad_status():
    with pytest.raises(ValidationError):
        TranscriptResult(video_id="x", status="done")  # job-internal, not a result status


def test_segment_roundtrip():
    s = Segment(start=1.5, duration=2.0, text="hello")
    assert s.text == "hello" and s.start == 1.5


def test_transcript_quality_mapping():
    assert TRANSCRIPT_QUALITY["caption_manual"] == "human-authored captions"
    assert "auto-captions" in transcript_quality_for("caption_auto")
    assert "ASR (Whisper)" in transcript_quality_for("whisper")
    assert set(TRANSCRIPT_QUALITY) == {"caption_manual", "caption_auto", "whisper"}


def test_whisper_job_defaults_and_assignment():
    job = WhisperJob(video_id="dQw4w9WgXcQ", created_at=1000.0)
    assert job.status == "pending"
    job.status = "running"
    assert job.status == "running"
    with pytest.raises(ValidationError):
        job.status = "bogus"


def test_egress_report():
    rep = EgressReport(ip="1.2.3.4", asn="AS7922", org="Comcast", is_residential=True)
    assert rep.is_residential is True
    assert rep.via_proxy is False
