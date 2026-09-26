"""Guards against documentation reference drift (bead ytt-86634a21).

Curated docs cite repo artifacts three ways — relative links, backticked
repo paths, and bead identifiers — and every class has rotted for real:
three deploy runbooks were committed (bead ytt-8f8356de) linking
``../docs/notes/retention-policy.md`` before that file existed, so the
committed tree carried links resolving to nothing until the companion doc
landed. A doc citation that names a path that does not exist reads as
authoritative and sends every reader (and every worker that trusts prose)
to a dead end. These tests make the reference classes mechanical:

* every relative link in a curated doc must resolve to an existing file or
  directory, and a link carrying a ``#fragment`` must name a real heading —
  anchors are validated with the GitHub slug algorithm (fence-aware,
  duplicate-aware), so a renamed section breaks the gate instead of
  silently stranding readers;
* every backticked ``tests/``, ``scripts/``, ``deploy/``, ``ytt/`` or
  ``docs/`` path must exist (text after ``:`` is dropped first, so pytest
  node ids pass). This is the leg that catches the wrong-extension class —
  ``test_whisper_contract.py`` cited as ``.pyi``, a test renamed out from
  under a runbook — because the cited path simply is not there;
* every ``ytt-XXXXXXXX`` bead identifier cited in a curated doc must exist
  in the live bead store. Same skip shape as the deploy-parity and
  inventory-freshness guards: clean extractions and CI image builds have no
  ``.beads/beads.db``, so that leg enforces only where the store exists.

Scope: ``README.md``, ``CONTRIBUTING.md``, ``SECURITY.md``, ``CHANGELOG.md``
and everything under ``docs/``.  ``docs/research/`` is exempt from the path
leg only — third-party material where "1.5 scripts/day" is prose about
throughput, not a file citation — and the root ``notes/`` per-bead
investigation records are out of scope entirely, matching the bead-status
lint's exemptions.  ``deploy/*.md`` runbooks are deliberately not scanned
yet: three of them carry the anticipatory retention-policy links above,
which resolve the moment the in-flight retention-policy doc (bead
ytt-5f43749f) lands; flipping them into scope then is a one-line change to
``CURATED_DOC_GLOBS``.

``scripts/definition-of-done.sh`` runs these as part of the unit suite.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Curated documentation: top-level markdown plus everything under docs/.
# deploy/*.md and root notes/ are excluded — see the module docstring.
CURATED_DOC_GLOBS = ("*.md", "docs/**/*.md")

# The path leg skips third-party research material: its prose legitimately
# contains strings like `scripts/day` that are rates, not file citations.
PATH_LEG_EXEMPT_DIR = REPO_ROOT / "docs" / "research"

# Repo-relative citation prefixes the path leg treats as file references.
REPO_PATH_PREFIXES = ("tests/", "scripts/", "deploy/", "ytt/", "docs/")

LINK_RE = re.compile(r"\]\(([^)\s]+)\)")
URI_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*:")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
FENCE_RE = re.compile(r"^(`{3,}|~{3,})")
REPO_PATH_RE = re.compile(
    r"`((?:" + "|".join(re.escape(p) for p in REPO_PATH_PREFIXES) + r")[\w./:-]+)`"
)
BEAD_ID_RE = re.compile(r"\bytt-[0-9a-f]{8}\b")


def _curated_docs() -> list[Path]:
    docs: set[Path] = set()
    for pattern in CURATED_DOC_GLOBS:
        docs.update(path for path in REPO_ROOT.glob(pattern) if path.is_file())
    assert docs, "no curated docs found — CURATED_DOC_GLOBS must be wrong"
    return sorted(docs)


def _github_slug(heading: str) -> str:
    """GitHub's anchor slug for heading text: strip markdown formatting,
    lowercase, drop everything that is not a word character, hyphen or
    space, then join the remaining runs with hyphens. A dropped character
    between spaces yields the doubled hyphen ("ownership & whisper" ->
    "ownership--whisper"); underscores are word characters and survive."""
    stripped = re.sub(r"[`*~\[\]()]", "", heading).strip().lower()
    return re.sub(r"[^\w\- ]", "", stripped).replace(" ", "-")


def _heading_slugs(path: Path) -> set[str]:
    """Anchor slugs of every ATX heading in *path*, skipping fenced code
    blocks and suffixing duplicate slugs GitHub-style (-1, -2, ...)."""
    slugs: list[str] = []
    seen: dict[str, int] = {}
    fence: str | None = None
    for line in path.read_text().splitlines():
        if fence is not None:
            if line.lstrip().startswith(fence):
                fence = None
            continue
        opening = FENCE_RE.match(line)
        if opening:
            fence = opening.group(1)[0] * 3
            continue
        heading = HEADING_RE.match(line)
        if heading:
            base = _github_slug(heading.group(2))
            count = seen.get(base, 0)
            seen[base] = count + 1
            slugs.append(base if count == 0 else f"{base}-{count}")
    return set(slugs)


def _rel(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


def test_relative_links_and_anchors_resolve():
    offenders: list[str] = []
    slug_cache: dict[str, set[str]] = {}
    for doc in _curated_docs():
        for match in LINK_RE.finditer(doc.read_text()):
            target = match.group(1)
            if URI_SCHEME_RE.match(target):
                continue  # external URL — existence is a network question
            path_part, _, anchor = target.partition("#")
            resolved = doc if not path_part else (doc.parent / path_part).resolve()
            if not resolved.exists():
                offenders.append(f"{_rel(doc)}: ({target}) — target does not exist")
                continue
            if not anchor or not resolved.is_file() or resolved.suffix != ".md":
                continue  # anchors only have slug semantics on markdown files
            slugs = slug_cache.setdefault(str(resolved), _heading_slugs(resolved))
            if anchor not in slugs:
                offenders.append(
                    f"{_rel(doc)}: ({target}) — no heading anchors to "
                    "'#" + anchor + "' (GitHub slug of the heading must match "
                    "exactly; check for renames or typos)"
                )
    assert not offenders, "\n".join(
        [
            "documentation links that do not resolve:",
            *(f"  - {offender}" for offender in offenders),
            "",
            "A committed link that resolves to nothing reads as "
            "authoritative and strands every reader. Fix the target path or "
            "anchor (or remove the link). Anchors are checked with GitHub's "
            "slug algorithm, so the anchor must equal the heading text "
            "lowercased with spaces as hyphens and punctuation dropped.",
        ]
    )


def test_backticked_repo_paths_exist():
    offenders: list[str] = []
    for doc in _curated_docs():
        if doc.is_relative_to(PATH_LEG_EXEMPT_DIR):
            continue
        for match in REPO_PATH_RE.finditer(doc.read_text()):
            cited = match.group(1)
            # `tests/unit/test_x.py::TestClass::test_case` — only the file
            # part of a pytest node id names a path.
            file_part = cited.partition(":")[0].rstrip("/")
            if (REPO_ROOT / file_part).exists():
                continue
            offenders.append(f"{_rel(doc)}: `{cited}` — no such path in the repo")
    assert not offenders, "\n".join(
        [
            "docs cite repo paths that do not exist:",
            *(f"  - {offender}" for offender in offenders),
            "",
            "Wrong-extension test citations, renamed scripts and moved "
            "manifests rot silently in prose. Fix the citation to the real "
            "path (or restore the path). Backticked tests/, scripts/, "
            "deploy/, ytt/ and docs/ references are checked verbatim; text "
            "after ':' (pytest node ids) is ignored.",
        ]
    )


def _live_bead_ids() -> set[str]:
    result = subprocess.run(
        ["bead", "list", "--json", "--limit", "10000"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, (
        f"bead list failed (exit {result.returncode}): "
        f"{result.stderr.strip()[:400]} — cited-bead validation cannot run"
    )
    ids = {
        json.loads(line)["id"] for line in result.stdout.splitlines() if line.strip()
    }
    assert ids, "bead list returned no beads — wrong workspace or CLI regression"
    return ids


def test_cited_bead_ids_exist_in_store():
    if not (shutil.which("bead") and (REPO_ROOT / ".beads" / "beads.db").exists()):
        pytest.skip(
            "no live bead store (bead CLI + .beads/beads.db) — cited-ID "
            "validity is enforced only on workspace hosts where the store "
            "exists; link and path legs still ran"
        )
    ids = _live_bead_ids()
    offenders: list[str] = []
    for doc in _curated_docs():
        for bead_id in sorted(set(BEAD_ID_RE.findall(doc.read_text()))):
            if bead_id not in ids:
                offenders.append(f"{_rel(doc)}: {bead_id}")
    assert not offenders, "\n".join(
        [
            "docs cite bead IDs that do not exist in the live store:",
            *(f"  - {offender}" for offender in offenders),
            "",
            "A fabricated or typo'd bead ID sends readers hunting for work "
            "that does not exist. Check the ID with `bead show <id>`; if the "
            "bead is real, the store this test queried may not be the one "
            "the doc was written against.",
        ]
    )
