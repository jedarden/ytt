"""Integration coverage for the bead-inventory regeneration decision logic.

These tests deliberately execute the repository's two shell scripts from a
temporary Git checkout. The fake ``bead`` command is only a JSONL-backed
store, while the real regeneration, comparison, commit, push, restore and
rollback logic remains under test.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
CADENCE_SOURCE = REPO_ROOT / "scripts" / "bead-inventory-cadence.sh"
REGEN_SOURCE = REPO_ROOT / "scripts" / "regen-bead-inventory.sh"

BASE_BEADS = [
    {
        "id": "ytt-fake-open",
        "title": "A seeded open bead",
        "status": "open",
        "effective_status": "open",
        "priority": 2,
        "assignee": None,
        "labels": ["fixture"],
        "manual_blocked": False,
        "dependencies": [],
        "revision": 1,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
    },
    {
        "id": "ytt-fake-closed",
        "title": "A seeded closed bead",
        "status": "closed",
        "effective_status": "closed",
        "priority": 3,
        "assignee": None,
        "labels": [],
        "manual_blocked": False,
        "dependencies": [],
        "revision": 2,
        "created_at": "2026-01-03T00:00:00Z",
        "updated_at": "2026-01-04T00:00:00Z",
    },
]

NEW_BEAD = {
    "id": "ytt-fake-new",
    "title": "A newly arrived bead",
    "status": "in_progress",
    "effective_status": "in_progress",
    "priority": 1,
    "assignee": "fixture-worker",
    "labels": ["fixture", "changed"],
    "manual_blocked": False,
    "dependencies": [],
    "revision": 1,
    "created_at": "2026-02-01T00:00:00Z",
    "updated_at": "2026-02-02T00:00:00Z",
}

FAKE_BEAD = """#!/usr/bin/env python3
import os
import sys
from pathlib import Path

log = os.environ.get("FAKE_BEAD_LOG")
if log:
    with Path(log).open("a") as stream:
        stream.write(" ".join(sys.argv[1:]) + "\\n")

if "--json" in sys.argv:
    sys.stdout.write(Path(os.environ["FAKE_BEAD_STORE"]).read_text())
"""


@dataclass
class Checkout:
    path: Path
    origin: Path
    store: Path
    bead_log: Path
    env: dict[str, str]
    cadence: Path
    regen: Path


def _run(
    args: list[str],
    cwd: Path,
    *,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        env=env,
        check=check,
        capture_output=True,
        text=True,
    )


def _write_store(store: Path, beads: list[dict]) -> None:
    store.write_text("".join(json.dumps(bead) + "\n" for bead in beads))


def _git(checkout: Checkout | Path, *args: str, env: dict[str, str] | None = None):
    path = checkout.path if isinstance(checkout, Checkout) else checkout
    return _run(["git", *args], path, env=env)


def _make_checkout(tmp_path: Path, *, commit_age_days: int = 0) -> Checkout:
    path = tmp_path / "checkout"
    path.mkdir()
    (path / "docs").mkdir()
    scripts = path / "scripts"
    scripts.mkdir()
    cadence = scripts / CADENCE_SOURCE.name
    regen = scripts / REGEN_SOURCE.name
    shutil.copy2(CADENCE_SOURCE, cadence)
    shutil.copy2(REGEN_SOURCE, regen)

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    bead = fake_bin / "bead"
    bead.write_text(FAKE_BEAD)
    bead.chmod(bead.stat().st_mode | stat.S_IXUSR)
    store = tmp_path / "fake-bead-store.jsonl"
    bead_log = tmp_path / "fake-bead.log"
    _write_store(store, BASE_BEADS)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["FAKE_BEAD_STORE"] = str(store)
    env["FAKE_BEAD_LOG"] = str(bead_log)

    _run(["git", "-c", "init.defaultBranch=master", "init"], path)
    _git(path, "config", "user.name", "fixture-worker")
    _git(path, "config", "user.email", "fixture@example.invalid")
    origin = tmp_path / "origin.git"
    _run(["git", "init", "--bare", str(origin)], tmp_path)
    # The fleet-wide global hook path also applies to temporary repositories;
    # point this bare fixture back at its own hooks so the rejection test is
    # deterministic and does not depend on host hooks.
    _run(
        ["git", "--git-dir", str(origin), "config", "core.hooksPath", "hooks"],
        tmp_path,
    )

    # Seed the committed pair through the real regenerator. All subsequent
    # decisions therefore compare against an actual generated snapshot.
    _run([str(regen)], path, env=env)
    _git(path, "add", "docs", "scripts")
    commit_env = env.copy()
    if commit_age_days:
        old = datetime.now(timezone.utc) - timedelta(days=commit_age_days)
        commit_env["GIT_AUTHOR_DATE"] = old.isoformat()
        commit_env["GIT_COMMITTER_DATE"] = old.isoformat()
    _run(["git", "commit", "-m", "seed generated inventory"], path, env=commit_env)
    _git(path, "remote", "add", "origin", str(origin))
    _git(path, "push", "--set-upstream", "origin", "master")
    bead_log.write_text("")
    return Checkout(path, origin, store, bead_log, env, cadence, regen)


def _set_beads(checkout: Checkout, beads: list[dict]) -> None:
    _write_store(checkout.store, beads)


def _force_generated_at(checkout: Checkout, generated_at: str) -> None:
    """Make the fixture's next regeneration timestamp deterministic."""
    date = checkout.path.parent / "fake-bin" / "date"
    real_date = shutil.which("date")
    assert real_date
    date.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-u" ] && [ "$2" = "+%Y-%m-%dT%H:%M:%SZ" ]; then\n'
        '  printf \'%s\\n\' "$FAKE_GENERATED_AT"\n'
        "else\n"
        '  exec "$REAL_DATE" "$@"\n'
        "fi\n"
    )
    date.chmod(date.stat().st_mode | stat.S_IXUSR)
    checkout.env["FAKE_GENERATED_AT"] = generated_at
    checkout.env["REAL_DATE"] = real_date


def _run_cadence(checkout: Checkout) -> subprocess.CompletedProcess[str]:
    return _run(
        [str(checkout.cadence), "run"],
        checkout.path,
        env=checkout.env,
        check=False,
    )


def _pair_bytes(checkout: Checkout) -> tuple[bytes, bytes]:
    return (
        (checkout.path / "docs/bead-inventory.md").read_bytes(),
        (checkout.path / "docs/bead-inventory.json").read_bytes(),
    )


def _head(checkout: Checkout) -> str:
    return _git(checkout, "rev-parse", "HEAD").stdout.strip()


def _status(checkout: Checkout) -> str:
    return _git(checkout, "status", "--porcelain").stdout


@pytest.mark.parametrize("staged", [False, True])
def test_dirty_snapshot_pair_is_skipped_without_calling_the_store(tmp_path, staged):
    checkout = _make_checkout(tmp_path)
    md = checkout.path / "docs/bead-inventory.md"
    md.write_text(md.read_text() + "\n# worker still editing this pair\n")
    if staged:
        _git(checkout, "add", "docs/bead-inventory.md")
    before = _pair_bytes(checkout)
    head = _head(checkout)

    result = _run_cadence(checkout)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "skipping this tick" in result.stdout
    assert _pair_bytes(checkout) == before
    assert _head(checkout) == head
    assert checkout.bead_log.read_text() == ""


def test_failed_regeneration_restores_the_committed_pair(tmp_path):
    checkout = _make_checkout(tmp_path)
    before = _pair_bytes(checkout)
    invalid_bead = dict(BASE_BEADS[0])
    del invalid_bead["title"]
    _set_beads(checkout, [invalid_bead])

    result = _run_cadence(checkout)

    assert result.returncode == 1
    assert "regeneration failed" in result.stderr
    assert _pair_bytes(checkout) == before
    assert _status(checkout) == ""


def test_timestamp_only_churn_is_discarded_after_metadata_stripping(tmp_path):
    checkout = _make_checkout(tmp_path)
    before_json = json.loads((checkout.path / "docs/bead-inventory.json").read_text())
    generated = datetime.fromisoformat(before_json["generated_at"].replace("Z", "+00:00"))
    _force_generated_at(
        checkout,
        (generated + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    before = _pair_bytes(checkout)
    head = _head(checkout)

    result = _run_cadence(checkout)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "quiet store" in result.stdout
    assert "nothing committed" in result.stdout
    assert _pair_bytes(checkout) == before
    assert _head(checkout) == head
    assert _status(checkout) == ""


def test_real_store_data_change_commits_and_pushes_the_pair(tmp_path):
    checkout = _make_checkout(tmp_path)
    head = _head(checkout)
    _set_beads(checkout, [*BASE_BEADS, NEW_BEAD])

    result = _run_cadence(checkout)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "snapshot data changed" in result.stdout
    assert _head(checkout) != head
    assert "ytt-fake-new" in (
        checkout.path / "docs/bead-inventory.json"
    ).read_text()
    assert _status(checkout) == ""
    assert _git(checkout, "rev-list", "origin/master..HEAD").stdout.strip() == ""


def test_quiet_store_is_throttled_before_the_seven_day_heartbeat(tmp_path):
    checkout = _make_checkout(tmp_path)
    before_json = json.loads((checkout.path / "docs/bead-inventory.json").read_text())
    generated = datetime.fromisoformat(before_json["generated_at"].replace("Z", "+00:00"))
    _force_generated_at(
        checkout,
        (generated + timedelta(seconds=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    head = _head(checkout)

    result = _run_cadence(checkout)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "< 7d heartbeat" in result.stdout
    assert _head(checkout) == head
    assert _status(checkout) == ""


def test_quiet_store_gets_a_heartbeat_after_seven_days(tmp_path):
    checkout = _make_checkout(tmp_path, commit_age_days=8)
    before_json = json.loads((checkout.path / "docs/bead-inventory.json").read_text())
    generated = datetime.fromisoformat(before_json["generated_at"].replace("Z", "+00:00"))
    _force_generated_at(
        checkout,
        (generated + timedelta(seconds=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    head = _head(checkout)

    result = _run_cadence(checkout)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "heartbeat due" in result.stdout
    assert _head(checkout) != head
    assert _status(checkout) == ""
    assert _git(checkout, "rev-list", "origin/master..HEAD").stdout.strip() == ""
    assert "scheduled regeneration" in _git(
        checkout, "log", "-1", "--format=%s"
    ).stdout


def test_rejected_push_rolls_back_the_cadence_commit_and_pair(tmp_path):
    checkout = _make_checkout(tmp_path)
    before = _pair_bytes(checkout)
    head = _head(checkout)
    _set_beads(checkout, [*BASE_BEADS, NEW_BEAD])

    hook = checkout.origin / "hooks/pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)

    result = _run_cadence(checkout)

    assert result.returncode == 1
    assert "publish failed — commit rolled back" in result.stderr
    assert _head(checkout) == head
    assert _pair_bytes(checkout) == before
    assert _status(checkout) == ""
    assert _git(checkout, "rev-list", "origin/master..HEAD").stdout.strip() == ""
