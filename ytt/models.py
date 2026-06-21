"""Data models (plan: Data Models + Per-status field matrix).

The tool request/result contract as pydantic models, plus the internal
``WhisperJob`` runtime record and the ``EgressReport`` self-test shape. Status,
mode, and source vocabularies are pinned to the plan's enums; the per-source
``transcript_quality`` strings are the plan's exact text.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# --- vocabularies (plan: "status (unified)", modes, source) -----------------
Status = Literal["ok", "partial", "pending", "running", "error"]
Mode = Literal["full", "chunk"]
Source = Literal["caption_manual", "caption_auto", "whisper"]
WhisperJobStatus = Literal["pending", "running", "done", "error"]

#: transcript_quality — one human-readable string per source (plan: exact text).
TRANSCRIPT_QUALITY: dict[str, str] = {
    "caption_manual": "human-authored captions",
    "caption_auto": "auto-captions — may contain errors, no punctuation/speaker labels",
    "whisper": "ASR (Whisper) — may contain errors, no speaker labels",
}


def transcript_quality_for(source: str) -> str:
    """Return the ``transcript_quality`` string for a ``source`` (KeyError-safe)."""
    return TRANSCRIPT_QUALITY[source]


class Segment(BaseModel):
    """A single caption/transcript segment (plan: Segment)."""

    start: float  # seconds
    duration: float  # seconds
    text: str


class TranscriptRequest(BaseModel):
    """Arguments to ``get_youtube_transcript`` (plan: TranscriptRequest).

    ``query`` is mutually exclusive with ``start``/``end`` (plan: response shape).
    """

    model_config = ConfigDict(extra="forbid")

    url: str
    lang: str | None = None
    mode: Mode = "full"
    cursor: str | None = None
    start: float | None = None
    end: float | None = None
    query: str | None = None

    @model_validator(mode="after")
    def _query_excludes_time_bounds(self) -> "TranscriptRequest":
        if self.query is not None and (self.start is not None or self.end is not None):
            raise ValueError("query is mutually exclusive with start/end")
        return self


class TranscriptResult(BaseModel):
    """Tool result envelope (plan: TranscriptResult + Per-status field matrix).

    Only ``video_id`` and ``status`` are always set; the rest are populated per
    the field matrix for each status. ``transcript_url`` is reserved/unset in v1
    (ADR-002: chunk-only).
    """

    video_id: str
    status: Status

    # transcript payload
    source: Source | None = None
    lang: str | None = None
    requested_lang: str | None = None
    available_langs: list[str] | None = None
    transcript_quality: str | None = None
    text: str | None = None
    segments: list[Segment] | None = None

    # metadata
    title: str | None = None
    channel: str | None = None
    duration_sec: float | None = None
    published: str | None = None

    # pagination
    offset: int | None = None
    total_chars: int | None = None
    is_final: bool | None = None
    next_cursor: str | None = None

    # async-job + reserved
    eta_sec: float | None = None
    transcript_url: str | None = None  # reserved/unset in v1

    # error
    error_code: str | None = None
    message: str | None = None


class WhisperJob(BaseModel):
    """Internal Whisper job record (plan: WhisperJob FSM).

    Mutable runtime state held in the in-memory registry. ``created_at`` is an
    epoch-seconds float; ``result_ref`` points at the cached ``<id>.whisper.txt``
    unit when ``status == "done"``.
    """

    model_config = ConfigDict(validate_assignment=True)

    video_id: str
    status: WhisperJobStatus = "pending"
    created_at: float
    started_at: float | None = None
    eta_sec: float | None = None
    duration_sec: float | None = None
    result_ref: str | None = None
    error_code: str | None = None
    message: str | None = None


class EgressReport(BaseModel):
    """Egress self-test result (plan: EgressReport).

    ``is_residential`` is derived (ipinfo.io returns org/ASN, not a flag) and is
    the concrete test behind the residential Proof Obligation.
    """

    ip: str
    asn: str | None = None
    org: str | None = None
    via_proxy: bool = False
    is_residential: bool = False
