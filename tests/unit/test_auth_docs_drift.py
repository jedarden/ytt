"""Auth-documentation drift guard (bead ytt-4283b5b5).

``docs/notes/auth.md`` carried the ADR-001-era registration guidance —
"manual Client ID / Secret **(or FastMCP self-issued tokens)**" — long after
ADR-003 replaced that design: the shipped auth stack (``YttOIDCProvider`` on
FastMCP's ``OIDCProxy``) federates to an upstream OIDC IdP, and
``build_auth_provider`` fail-closes at startup without
``YTT_OAUTH_CLIENT_ID``/``YTT_OAUTH_CLIENT_SECRET``. A reader following the
drifted bullet would go looking for a self-issued registration path that
does not exist, while the code explicitly rejects that fallback ("must never
fall back to an unauthenticated or self-issued-with-no-login provider"),
pinned end-to-end by ``tests/unit/test_oauth_startup_fail_closed.py``.

The reconciliation decision (bead ytt-4283b5b5): self-issued tokens are
**not supported**, and the option is removed from the guidance docs rather
than implemented — ytt as its own AS with no upstream login is exactly the
design ADR-003 deliberately superseded. These tests keep the reconciliation
from silently reverting:

1. **No guidance doc offers the option.** A "self-issued" mention in the
   operational-guidance set (README, CONTRIBUTING, SECURITY,
   ``docs/notes/**``, ``docs/usage/**``) may appear only inside
   ``docs/notes/auth.md``'s reconciliation bullet. Historical surfaces are
   deliberately out of scope: ``docs/plan/plan.md`` is the ADR log (ADR-001
   and its supersession are history to preserve, and CONTRIBUTING forbids
   editing it), ``CHANGELOG.md`` records what shipped, ``docs/research/``
   is third-party survey, and root ``notes/`` are per-bead investigation
   records — the same exemptions the reference-drift and bead-status lints
   use.
2. **The reconciliation statement stays.** auth.md keeps a self-issued
   mention that names both ADRs, carries a supersession qualifier, and
   cites the fail-closed pin — losing any of those reopens the drift.
3. **Docs and code reject the same fallback for the same reason.** auth.md
   still names the startup-required client pair, and the gate in
   ``ytt/auth.py`` still names the self-issued fallback in its rejection —
   if either side moves, both must move in the same commit.

``scripts/definition-of-done.sh`` runs these as part of the unit suite.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
AUTH_MD = REPO_ROOT / "docs" / "notes" / "auth.md"
AUTH_SOURCE = REPO_ROOT / "ytt" / "auth.py"

#: The operational-guidance set — the documents a reader consults to decide
#: how to register a client or connect a connector.  Deliberately excludes
#: the historical surfaces (plan.md ADR log, CHANGELOG, docs/research/,
#: root notes/) — see the module docstring.
GUIDANCE_DOC_GLOBS: tuple[str, ...] = (
    "README.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "docs/notes/**/*.md",
    "docs/usage/**/*.md",
)

SELF_ISSUED_RE = re.compile(r"(?i)self-issued")

#: Wording that marks a self-issued mention as historical or rejected, as
#: opposed to offering it as an available path.
SUPERSESSION_QUALIFIER_RE = re.compile(
    r"(?i)never implemented|superseded|not supported|unsupported"
)

#: The pair whose absence is what makes self-issued operation impossible.
STARTUP_REQUIRED_PAIR = ("YTT_OAUTH_CLIENT_ID", "YTT_OAUTH_CLIENT_SECRET")


def _guidance_docs() -> list[Path]:
    docs: set[Path] = set()
    for pattern in GUIDANCE_DOC_GLOBS:
        docs.update(path for path in REPO_ROOT.glob(pattern) if path.is_file())
    assert docs, "no guidance docs found — GUIDANCE_DOC_GLOBS must be wrong"
    return sorted(docs)


def test_no_guidance_doc_offers_self_issued_tokens():
    """'self-issued' may appear in the guidance set only inside auth.md's
    reconciliation bullet — anywhere else reads as an available path, which
    is the drift this guard exists for (bead ytt-4283b5b5)."""
    offenders: list[str] = []
    for doc in _guidance_docs():
        if doc == AUTH_MD:
            continue  # the sanctioned reconciliation home (leg 2)
        for lineno, line in enumerate(doc.read_text().splitlines(), start=1):
            if SELF_ISSUED_RE.search(line):
                offenders.append(
                    f"{doc.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}"
                )
    assert not offenders, "\n".join(
        [
            "'self-issued' appeared outside docs/notes/auth.md:",
            *(f"  - {offender}" for offender in offenders),
            "",
            "FastMCP self-issued tokens are NOT a supported registration "
            "path: ytt federates to an upstream OIDC IdP (ADR-003) and "
            "build_auth_provider fail-closes at startup without the "
            "YTT_OAUTH_CLIENT_ID / YTT_OAUTH_CLIENT_SECRET pair. Presenting "
            "them as an option is documentation drift. Keep any mention "
            "inside docs/notes/auth.md's reconciliation bullet (stating the "
            "ADR-001 supersession), or land the design change that actually "
            "implements the flow — with its own tests — before documenting "
            "it as available.",
        ]
    )


def test_auth_md_keeps_the_supersession_statement():
    """auth.md's self-issued mention must stay a reconciliation: both ADRs,
    a supersession qualifier, and the fail-closed pin citation."""
    lines = [
        line
        for line in AUTH_MD.read_text(encoding="utf-8").splitlines()
        if SELF_ISSUED_RE.search(line)
    ]
    assert lines, (
        "docs/notes/auth.md no longer mentions self-issued tokens — the "
        "reconciliation statement was removed. The decision (supported vs "
        "not) must stay documented either way: restore the supersession "
        "bullet, or, if self-issued tokens are now a supported path, "
        "rewrite it as an implemented feature with its own tests and update "
        "this guard."
    )
    block = "\n".join(lines)
    for adr in ("ADR-001", "ADR-003"):
        assert adr in block, (
            f"auth.md's self-issued reconciliation stopped naming {adr} — "
            "the supersession history is what makes the 'not supported' "
            "verdict checkable"
        )
    assert SUPERSESSION_QUALIFIER_RE.search(block), (
        "auth.md's self-issued mention lost its supersession qualifier "
        "(never implemented / superseded / not supported) — without it the "
        "mention reads as an available registration path again"
    )
    assert "tests/unit/test_oauth_startup_fail_closed.py" in AUTH_MD.read_text(
        encoding="utf-8"
    ), (
        "auth.md stopped citing tests/unit/test_oauth_startup_fail_closed.py "
        "— the fail-closed pin is the enforcement half of the "
        "reconciliation"
    )


def test_auth_md_names_the_startup_required_client_pair():
    """The startup-required pair is *why* self-issued operation is
    impossible; the reconciliation bullet must keep naming both halves."""
    text = AUTH_MD.read_text(encoding="utf-8")
    for var in STARTUP_REQUIRED_PAIR:
        assert var in text, (
            f"auth.md stopped naming {var} — the fail-closed credential "
            "pair is what makes self-issued operation impossible, and the "
            "reconciliation has to say so"
        )


def test_auth_gate_still_rejects_the_self_issued_fallback():
    """The code half of the reconciliation: ytt/auth.py's gate message still
    names the startup-required id and still rejects the self-issued
    fallback by name. If the gate's wording changes — or the gate itself —
    re-reconcile the docs in the same commit."""
    source = AUTH_SOURCE.read_text(encoding="utf-8")
    assert "YTT_OAUTH_CLIENT_ID is required" in source, (
        "build_auth_provider's fail-closed gate message changed — "
        "docs/notes/auth.md's reconciliation cites it; update both in the "
        "same commit"
    )
    assert SELF_ISSUED_RE.search(source), (
        "ytt/auth.py no longer names the self-issued fallback — if "
        "self-issued operation is now intended, that is a design change "
        "(a new ADR superseding ADR-003) and the guidance docs must be "
        "re-reconciled deliberately, not drifted"
    )
