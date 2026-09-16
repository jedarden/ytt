"""Byte-for-byte parity between the ``deploy/`` mirror and declarative-config.

``deploy/README.md`` defines ``deploy/k8s/`` as a read-only mirror of the
manifests that are actually applied, living in ``jedarden/declarative-config``
(``k8s/`` path, synced by ArgoCD).  Drift happened once for real (bead
ytt-15205fb4: after the public image repoint the mirror sat a full revision
behind applied state), so parity is asserted here rather than left to review
discipline — this is the guard ``docs/notes/single-replica.md`` points at for
"keep the two copies identical".

Mapping (``deploy/README.md`` §Layout):

* ``ardenone-cluster/ytt`` is regenerated with ``rsync -a --delete``, so the
  mirror must equal that declarative-config directory exactly — same file
  *set*, byte-identical contents, both directions.
* every other mirrored file (the two iad-ci files) is copied individually:
  it must exist at the same relative path in declarative-config and be
  byte-identical, but declarative-config's iad-ci directories legitimately
  hold ~180 non-ytt neighbours, so no set equality on that side.

The declarative-config checkout is located via ``$YTT_DECLARATIVE_CONFIG_DIR``
or the sibling ``../declarative-config`` of this repo.  Without a checkout
(CI image builds exclude ``deploy/`` and have no sibling) the whole module
skips: the guard runs wherever the applied manifests are actually checked
out and edits are validated (``scripts/definition-of-done.sh`` runs it
first, separately).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_K8S = REPO_ROOT / "deploy" / "k8s"

# Trees regenerated with `rsync -a --delete` (deploy/README.md): the mirror
# must be *exactly* these declarative-config directories — no missing and no
# extra files.  Relative to both deploy/k8s/ and the dc checkout's k8s/.
RSYNCED_TREES = (Path("ardenone-cluster") / "ytt",)

# Individually copied mirror files (deploy/README.md §Layout).  Listed so a
# deletion from the mirror cannot pass vacuously — one-directional presence
# checks alone would not notice a file disappearing from deploy/k8s.
COPIED_FILES = (
    Path("iad-ci/argo-workflows/ytt-build.yaml"),
    Path("iad-ci/argo-events/ytt-sensor.yml"),
)


def _find_declarative_config() -> Path | None:
    env_dir = os.environ.get("YTT_DECLARATIVE_CONFIG_DIR")
    if env_dir:
        root = Path(env_dir).expanduser()
        if not (root / "k8s").is_dir():
            pytest.fail(
                f"YTT_DECLARATIVE_CONFIG_DIR={env_dir!r} does not look like a "
                "declarative-config checkout (no k8s/ directory)"
            )
        return root.resolve()
    sibling = REPO_ROOT.parent / "declarative-config"
    if (sibling / "k8s").is_dir():
        return sibling.resolve()
    return None


DC_ROOT = _find_declarative_config()
if DC_ROOT is None:
    pytest.skip(
        "no declarative-config checkout found (checked "
        "$YTT_DECLARATIVE_CONFIG_DIR and ../declarative-config) — the deploy/ "
        "mirror parity guard only runs where the applied manifests are "
        "checked out; point YTT_DECLARATIVE_CONFIG_DIR at one to enable it",
        allow_module_level=True,
    )

DC_K8S = DC_ROOT / "k8s"


def _dc_head() -> str:
    """Best-effort short hash of the dc checkout, for failure messages."""
    try:
        out = subprocess.run(
            ["git", "-C", str(DC_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return out.stdout.strip() or "unknown"


def test_documented_mirror_files_exist():
    for rel in COPIED_FILES:
        assert (DEPLOY_K8S / rel).is_file(), (
            f"deploy/k8s/{rel} is listed in deploy/README.md §Layout but is "
            "missing from the mirror — either restore it from "
            "declarative-config or update the Layout table (and "
            "COPIED_FILES) together"
        )


def test_mirror_has_no_undocumented_files():
    allowed_roots = tuple(DEPLOY_K8S / tree for tree in RSYNCED_TREES)
    copied = set(COPIED_FILES)
    strays = [
        path.relative_to(DEPLOY_K8S)
        for path in DEPLOY_K8S.rglob("*")
        if path.is_file()
        and not any(path.is_relative_to(root) for root in allowed_roots)
        and path.relative_to(DEPLOY_K8S) not in copied
    ]
    assert not strays, (
        f"unexpected files under deploy/k8s outside the documented mirror "
        f"({', '.join(map(str, strays))}) — every file here must appear in "
        "deploy/README.md §Layout; add it there (and to COPIED_FILES or "
        "RSYNCED_TREES) or remove it"
    )


def test_deploy_mirror_matches_declarative_config():
    problems: list[str] = []

    # Every mirrored file must have a byte-identical dc counterpart.
    for path in sorted(p for p in DEPLOY_K8S.rglob("*") if p.is_file()):
        rel = path.relative_to(DEPLOY_K8S)
        dc_file = DC_K8S / rel
        if not dc_file.is_file():
            problems.append(f"deploy/k8s/{rel}: no counterpart at {dc_file}")
        elif path.read_bytes() != dc_file.read_bytes():
            problems.append(f"deploy/k8s/{rel}: differs from {dc_file}")

    # rsync --delete trees: a dc-side file with no mirror is drift too.
    for tree in RSYNCED_TREES:
        dc_tree = DC_K8S / tree
        if not dc_tree.is_dir():
            problems.append(f"{dc_tree}: missing from declarative-config")
            continue
        mirrored = {
            p.relative_to(DEPLOY_K8S)
            for p in (DEPLOY_K8S / tree).rglob("*")
            if p.is_file()
        }
        problems.extend(
            f"{dc_path.relative_to(DC_K8S)}: exists in declarative-config "
            "but is not mirrored under deploy/k8s"
            for dc_path in sorted(p for p in dc_tree.rglob("*") if p.is_file())
            if dc_path.relative_to(DC_K8S) not in mirrored
        )

    assert not problems, "\n".join(
        [
            f"deploy/ mirror has drifted from the applied manifests in "
            f"{DC_ROOT} (git {_dc_head()}):",
            *(f"  - {problem}" for problem in problems),
            "",
            "declarative-config is the source of truth — regenerate the "
            "mirror with the rsync/cp commands in deploy/README.md, then "
            "commit the refreshed mirror alongside the declarative-config "
            "change that prompted it.",
        ]
    )
