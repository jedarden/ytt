"""No-Whisper section of docs/usage/self-hosting.md — doc drift guard.

Final slice of the caption-only chain (bead ``ytt-78a2c82e``): the
operator-facing No-Whisper section must keep describing the failure shape
the rest of the chain pinned, not the shape an earlier draft imagined.

What the chain pinned, and where:

- ``test_no_whisper_boot.py`` — an unset ``YTT_WHISPER_URL`` resolves to
  the declared reference default; unset never means disabled;
- ``test_no_whisper_captions.py`` — captioned videos answer ``ok`` with
  ``source=caption_auto`` under every no-Whisper env shape (unset, empty);
- ``test_no_whisper_captionless.py`` — a caption-less video under
  ``YTT_WHISPER_URL=""`` starts ``pending`` with no ``error_code`` field
  and ends ``asr_failed`` at ``get_transcript_job``; the label
  ``no_captions_asr_failed`` (``ytt/errors.py``: WhisperJob-internal /
  metric-only) never surfaces as a tool ``error_code`` — the contract
  ``docs/usage/tools.md`` states under "``no_captions_asr_failed`` is not
  a tool error code".

Those modules pin the behavior; this module pins the doc, in the
``test_runbook_quotes_the_facts_it_depends_on`` pattern
(``tests/unit/test_asr_runbook.py``): if a doc refactor drops a literal an
operator needs — or resurrects the old, wrong response-body shape — the
suite fails and forces the doc fix in the same commit.
"""

from __future__ import annotations

import re
from pathlib import Path

DOC_DIR = Path(__file__).resolve().parents[2] / "docs" / "usage"
SELF_HOSTING = DOC_DIR / "self-hosting.md"
TOOLS = DOC_DIR / "tools.md"


def _no_whisper_section() -> str:
    """The ``## No-Whisper mode`` section, up to the next ``## `` heading."""
    doc = SELF_HOSTING.read_text(encoding="utf-8")
    start = doc.index("## No-Whisper mode")
    end = doc.index("\n## ", start + 1)
    return doc[start:end]


def test_section_quotes_the_pinned_no_whisper_contract() -> None:
    """The section must keep carrying every fact the chain pinned.

    A doc refactor that dropped the two-step failure shape, the
    metrics-only caveat (with its tools.md anchor), or the three
    ``YTT_WHISPER_URL`` shapes would leave operators with a section that
    describes a response the tool never returns.
    """
    section = _no_whisper_section()
    for literal in (
        # the start never probes ASR and carries no error_code (§ captionless pin)
        "get_youtube_transcript",
        '`status="pending"`',
        "no `error_code` field",
        # the poll carries the job's real code (§ tools.md error-code table)
        "get_transcript_job",
        '`error_code="asr_failed"`',
        # the label is metrics-only, never a tool error_code, with the anchor
        "no_captions_asr_failed",
        "labels metrics only",
        "tools.md#no_captions_asr_failed-is-not-a-tool-error-code",
        # the three env shapes: disabled deliberately, or reference default
        "YTT_WHISPER_URL",
        "unreachable address",
        "empty value",
        "captions are unaffected",
        "selects the built-in reference-endpoint",
        # where the behavior is pinned
        "tests/unit/test_caption_only_no_whisper.py",
    ):
        assert literal in section, f"No-Whisper section lost {literal!r}"


def test_section_never_shows_the_label_as_a_tool_response() -> None:
    """The old, wrong shape must stay out of the section.

    Older copies showed ``{"status": "error", "error_code":
    "no_captions_asr_failed", ...}`` as the tool response; the label is
    metrics-only (``ytt/errors.py``), so any error_code-pairing of it —
    in any quoting — is that lie coming back.
    """
    section = _no_whisper_section()
    assert not re.search(r'"error_code"\s*:\s*"no_captions_asr_failed"', section), (
        "No-Whisper section shows no_captions_asr_failed as a response "
        "error_code — it is a metrics-only label (see tools.md)"
    )


def test_section_links_to_a_real_tools_md_anchor() -> None:
    """The tools.md anchor the section cites must keep existing."""
    heading = "### `no_captions_asr_failed` is not a tool error code"
    assert heading in TOOLS.read_text(encoding="utf-8"), (
        f"self-hosting.md links to the tools.md anchor for {heading!r}; "
        "rename the section and the link in the same commit"
    )
