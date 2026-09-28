"""Keep the tool error-code namespace and ASR disclosures drift-guarded.

The stable table in ``docs/usage/tools.md`` is the client contract.  The
canonical set lives in ``ytt.errors`` so a new code has to update both the
implementation-facing registry and the documentation.  This is deliberately
separate from the canary verdict guard: canary outcomes include ``ok`` and
yt-dlp classifications, while this module covers ``TranscriptResult`` codes
and the metric-only Whisper labels.
"""

from __future__ import annotations

import re
from pathlib import Path

from ytt import errors

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_DOC = REPO_ROOT / "docs" / "usage" / "tools.md"
README = REPO_ROOT / "README.md"
REFERENCE_ASR = REPO_ROOT / "docs" / "notes" / "reference-asr.md"
SERVER = REPO_ROOT / "ytt" / "server.py"

_ERROR_TABLE_ROW = re.compile(r"^\|\s*`(?P<code>[a-z][a-z0-9_]*)`\s*\|", re.MULTILINE)


def _documented_tool_error_codes() -> set[str]:
    """Read the stable-code table, not incidental examples elsewhere in the doc."""
    document = TOOLS_DOC.read_text(encoding="utf-8")
    start = document.index("## Stable error codes")
    end = document.index("### `no_captions_asr_failed`", start)
    table = document[start:end]
    codes = {
        match.group("code")
        for match in _ERROR_TABLE_ROW.finditer(table)
        if match.group("code") != "error_code"
    }
    assert codes, "the stable error-code table disappeared from tools.md"
    return codes


def test_documented_tool_error_codes_equal_the_canonical_set() -> None:
    """Adding, removing, or renaming a tool code requires a doc update."""
    assert _documented_tool_error_codes() == set(errors.TOOL_ERROR_CODES)


def test_metric_only_labels_are_a_disjoint_namespace() -> None:
    """Whisper metric/bookkeeping labels can never become tool error codes."""
    assert errors.METRIC_ONLY_LABELS == {
        errors.NO_CAPTIONS_ASR_STARTED,
        errors.NO_CAPTIONS_ASR_FAILED,
    }
    assert errors.METRIC_ONLY_LABELS.isdisjoint(errors.TOOL_ERROR_CODES)
    assert errors.METRIC_ONLY_LABELS.isdisjoint(_documented_tool_error_codes())


def test_readme_and_reference_asr_name_the_code_boundary() -> None:
    """The reference-ASR failure wording follows the poller's real fallback."""
    readme = README.read_text(encoding="utf-8")
    reference_note = REFERENCE_ASR.read_text(encoding="utf-8")
    poller = SERVER.read_text(encoding="utf-8").split(
        "async def get_transcript_job", 1
    )[1]

    # Keep the literal stable code and the source-level fallback tied together.
    assert errors.ASR_FAILED == "asr_failed"
    assert 'error_code": job.error_code or errors.ASR_FAILED' in poller
    assert 'error_code="asr_failed"' in readme
    assert 'error_code="asr_failed"' in reference_note

    for document_name, document in (
        ("README", readme),
        ("reference-ASR note", reference_note),
    ):
        lowered = document.lower()
        assert errors.NO_CAPTIONS_ASR_FAILED in document, (
            f"{document_name} lost the metric-only label name"
        )
        assert "metrics-only" in lowered, (
            f"{document_name} lost the metrics-only boundary wording"
        )
        assert "not" in lowered and "tool" in lowered and "error_code" in lowered, (
            f"{document_name} no longer says the label is outside tool error_code"
        )
