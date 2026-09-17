"""Documentation-coverage guards for the environment-variable reference.

``docs/usage/configuration.md`` is the full reference and ``README.md``'s
Configuration table the curated quick reference; both must track
:class:`ytt.config.Settings` or a new ``YTT_*`` setting ships undocumented.
Two pins:

- the configuration guide carries a table row for **every** Settings field;
- both documents carry rows for the per-subject limit settings
  (``ALLOWED_SUBJECTS`` / ``RATE_LIMIT_PER_MIN`` / ``RATE_LIMIT_BURST`` /
  ``WHISPER_JOBS_PER_HOUR`` / ``MAX_CONCURRENT_WHISPER``), with their numeric
  defaults stated identically to the model — so a default change in
  ``ytt/config.py`` fails here until the docs follow.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ytt.config import Settings

_REPO_ROOT = Path(__file__).resolve().parents[2]
_README = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
_GUIDE = (_REPO_ROOT / "docs" / "usage" / "configuration.md").read_text(
    encoding="utf-8"
)

#: Settings the per-subject limiting docs must cover in BOTH documents.
_LIMIT_VARS = (
    "YTT_ALLOWED_SUBJECTS",
    "YTT_RATE_LIMIT_PER_MIN",
    "YTT_RATE_LIMIT_BURST",
    "YTT_WHISPER_JOBS_PER_HOUR",
    "YTT_MAX_CONCURRENT_WHISPER",
)


def _env_vars() -> dict[str, object]:
    """Map every Settings field's ``YTT_*`` env name to its model default."""
    return {
        f"YTT_{name.upper()}": field.default
        for name, field in Settings.model_fields.items()
    }


def _default_cell(doc: str, var: str) -> str | None:
    """Default cell of *var*'s table row in *doc* (``None`` if no row)."""
    m = re.search(rf"^\|\s*`{re.escape(var)}`\s*\|\s*([^|]+)\|", doc, re.MULTILINE)
    return m.group(1).strip() if m else None


def test_configuration_guide_has_a_row_for_every_setting():
    missing = sorted(
        var for var in _env_vars() if _default_cell(_GUIDE, var) is None
    )
    assert not missing, f"undocumented in docs/usage/configuration.md: {missing}"


def test_both_documents_cover_the_per_subject_limit_settings():
    for var in _LIMIT_VARS:
        assert _default_cell(_README, var) is not None, f"no README table row: {var}"
        assert _default_cell(_GUIDE, var) is not None, (
            f"no configuration.md table row: {var}"
        )


@pytest.mark.parametrize(
    "var,field",
    [
        ("YTT_RATE_LIMIT_PER_MIN", "rate_limit_per_min"),
        ("YTT_WHISPER_JOBS_PER_HOUR", "whisper_jobs_per_hour"),
        ("YTT_MAX_CONCURRENT_WHISPER", "max_concurrent_whisper"),
    ],
)
def test_numeric_limit_defaults_match_the_model(var, field):
    """A default changed in Settings must be mirrored in both documents."""
    expected = str(getattr(Settings.model_fields[field], "default", None))
    for doc_name, doc in (("README.md", _README), ("configuration.md", _GUIDE)):
        cell = _default_cell(doc, var)
        assert cell is not None, f"no {doc_name} table row: {var}"
        assert expected in cell, (
            f"{doc_name} states default {cell!r} for {var}; model says {expected}"
        )


def test_rate_limit_burst_documented_as_resolving_to_the_rate():
    """``rate_limit_burst`` defaults to None -> resolves to the per-min rate."""
    assert Settings.model_fields["rate_limit_burst"].default is None
    for doc_name, doc in (("README.md", _README), ("configuration.md", _GUIDE)):
        cell = _default_cell(doc, "YTT_RATE_LIMIT_BURST")
        assert cell is not None, f"no {doc_name} table row: YTT_RATE_LIMIT_BURST"
        assert "= rate" in cell or "=rate" in cell, (
            f"{doc_name} should state YTT_RATE_LIMIT_BURST defaults to the "
            f"rate; default cell is {cell!r}"
        )
