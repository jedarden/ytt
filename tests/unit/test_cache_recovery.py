"""Recovery smoke — the service recovers when cached data is unavailable.

Unit-level pin of the operator contract documented in
``deploy/CACHE-RUNBOOK.md`` (bead ``ytt-1ff7ef2e``): every recovery procedure
in that runbook leans on one of these behaviors actually holding, so each
scenario an operator relies on is exercised here, end to end, at the unit
level.  The repo has no staging cluster (RUNBOOK §10 there), so this file is
the sanctioned recovery drill.

Scenarios (and the runbook section each protects):

- wiped volume rebuilds organically          (§10 — post-incident steady state)
- backup → restore → re-scan round-trip      (§2/§3 — file-level tar copy)
- partial restore: missing sidecar           (§3 — torn unit degrades)
- partial restore: corrupt sidecar           (§3 — torn unit degrades)
- registered unit vanishes under a live cache(§6 — in-place cleanup is safe)
- ENOSPC exhaustion: degrade w/o residue,    (§4/§6 — full volume is a caching
  then recovers once space frees              outage, not a serving outage)
- backup glob excludes lockfile; a stale     (§1/§6 — never touch/restore
  lockfile on a restored volume is inert      .ytt-singleton.lock)
- runbook ↔ manifest drift guard             (§4/§7 — facts the doc quotes)

The last one follows the repo's docs-pin pattern (``test_docs_env_coverage``,
``test_single_replica``): the runbook quotes manifest values, so a manifest
change fails here until the doc follows — the runbook can't silently drift
into documenting a deployment that no longer exists.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml  # via fastmcp (runtime dependency) — always present in the venv

from ytt.cache import TranscriptCache, _unit_stems

CAP = 4096  # bytes — room for several small units, small enough to reason about
VIDEO_ID = "aaaabbbbccc"    # 11 chars
VIDEO_ID_2 = "ddddeeeefff"

REPO_ROOT = Path(__file__).resolve().parents[2]
CACHE_RUNBOOK = REPO_ROOT / "deploy" / "CACHE-RUNBOOK.md"
MANIFEST_DIR = REPO_ROOT / "deploy" / "k8s" / "ardenone-cluster" / "ytt"


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


def _cache(cache_dir: Path) -> TranscriptCache:
    return TranscriptCache(cache_dir, max_bytes=CAP, reconcile_sec=0)


def _write_unit(
    cache_dir: Path,
    video_id: str,
    lang: str,
    text: str = "hello",
    source: str = "caption_auto",
    segments: list[dict[str, Any]] | None = None,
) -> None:
    """Write a unit directly to disk, bypassing cache logic (restore shape)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    txt_name, json_name = _unit_stems(video_id, lang)
    (cache_dir / txt_name).write_text(text, encoding="utf-8")
    sidecar: dict[str, Any] = {"source": source}
    if segments is not None:
        sidecar["segments"] = segments
    (cache_dir / json_name).write_text(json.dumps(sidecar), encoding="utf-8")


def _backup_volume(cache_dir: Path, backup_dir: Path) -> list[Path]:
    """The runbook §2 backup contract: flat copy of *.txt + *.json, nothing else."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    for pattern in ("*.txt", "*.json"):
        for src in cache_dir.glob(pattern):
            dst = backup_dir / src.name
            shutil.copy2(src, dst)  # copy2 preserves mtimes (runbook §3)
            copied.append(dst)
    return copied


async def _put(cache: TranscriptCache, video_id: str, lang: str, text: str,
               segments: list[dict[str, Any]] | None = None) -> bool:
    return await cache.put(video_id, lang, text, segments, "caption_auto", None)


# --------------------------------------------------------------------------- #
# Wiped volume rebuilds organically                                            #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_wiped_volume_rebuilds_organically(tmp_path: Path) -> None:
    """A lost/recreated PVC must degrade to plain misses, then rebuild.

    The post-incident steady state (CACHE-RUNBOOK §10): an empty cache dir is
    not an error state — every get misses without raising, the first put
    re-seeds, and the next get hits.
    """
    volume = tmp_path / "recreated-pvc"  # never exists — dir creation is on scan
    cache = _cache(volume)

    await cache.startup_scan()
    assert cache.unit_count == 0
    assert cache.total_bytes == 0
    assert await cache.get(VIDEO_ID, "en") is None  # miss, not a crash

    assert await _put(cache, VIDEO_ID, "en", "rebuilt") is True
    hit = await cache.get(VIDEO_ID, "en")
    assert hit is not None and hit.text == "rebuilt"
    assert volume.is_dir()  # scan created the mount point


# --------------------------------------------------------------------------- #
# Backup → restore → re-scan round-trip                                        #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_backup_restore_roundtrip(tmp_path: Path) -> None:
    """A file-level backup restored onto a fresh volume re-scans warm.

    This is the post-``ytt-4f1c45c2`` contract (CACHE-RUNBOOK §3): startup_scan
    re-registers restored units and serves byte-identical content — text,
    source, segments, metadata all survive as flat files.
    """
    live = tmp_path / "live"
    cache = _cache(live)
    await cache.startup_scan()
    segments = [{"text": "seg one", "start": 0.0}]
    await _put(cache, VIDEO_ID, "en", "english text", segments=segments)
    await cache.put(VIDEO_ID_2, "de", "german text", None, "whisper", {"ttl": 1})

    backup_dir = tmp_path / "backup"
    _backup_volume(live, backup_dir)

    restored = _cache(backup_dir)  # same flat files, fresh process/inventory
    await restored.startup_scan()
    assert restored.unit_count == 2

    hit_en = await restored.get(VIDEO_ID, "en")
    assert hit_en is not None
    assert hit_en.text == "english text"
    assert hit_en.source == "caption_auto"
    assert hit_en.segments == segments

    hit_de = await restored.get(VIDEO_ID_2, "de")
    assert hit_de is not None
    assert hit_de.source == "whisper"
    assert hit_de.metadata == {"ttl": 1}


@pytest.mark.asyncio
async def test_restore_missing_sidecar_still_serves(tmp_path: Path) -> None:
    """A torn unit — .txt restored without its .json — serves, doesn't error."""
    volume = tmp_path / "restored"
    _write_unit(volume, VIDEO_ID, "en", text="body only")
    (volume / _unit_stems(VIDEO_ID, "en")[1]).unlink()  # drop the sidecar

    cache = _cache(volume)
    await cache.startup_scan()
    hit = await cache.get(VIDEO_ID, "en")
    assert hit is not None
    assert hit.text == "body only"
    assert hit.source == "caption_auto"  # documented default (runbook §3)
    assert hit.segments is None


@pytest.mark.asyncio
async def test_restore_corrupt_sidecar_still_serves(tmp_path: Path) -> None:
    """A corrupt sidecar is ignored — the text half still serves."""
    volume = tmp_path / "restored"
    _write_unit(volume, VIDEO_ID, "en", text="survives")
    txt_name, json_name = _unit_stems(VIDEO_ID, "en")
    (volume / json_name).write_bytes(b"{not json at all")

    cache = _cache(volume)
    await cache.startup_scan()
    hit = await cache.get(VIDEO_ID, "en")
    assert hit is not None
    assert hit.text == "survives"
    assert hit.source == "caption_auto"


# --------------------------------------------------------------------------- #
# In-place cleanup is safe: registered unit vanishing under a live cache       #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_registered_unit_vanishing_degrades_to_miss_then_recaches(
    tmp_path: Path,
) -> None:
    """Manual unit deletion (runbook §6.3) must not wedge the running cache.

    The §6 recovery deletes files under a live server.  Contract: the first
    access after an external deletion degrades to a clean miss (registry entry
    dropped, byte counter corrected — no phantom bytes), and the unit
    re-caches normally afterwards.
    """
    volume = tmp_path / "live"
    cache = _cache(volume)
    await cache.startup_scan()
    await _put(cache, VIDEO_ID, "en", "doomed")
    assert cache.total_bytes > 0

    txt_name, json_name = _unit_stems(VIDEO_ID, "en")
    (volume / txt_name).unlink()
    (volume / json_name).unlink()

    assert await cache.get(VIDEO_ID, "en") is None  # miss, registry self-corrects
    assert cache.unit_count == 0
    assert cache.total_bytes == 0  # no phantom accounting after cleanup

    await _put(cache, VIDEO_ID, "en", "recached")  # the recovery loop closes
    hit = await cache.get(VIDEO_ID, "en")
    assert hit is not None and hit.text == "recached"


# --------------------------------------------------------------------------- #
# ENOSPC exhaustion: degrade without residue, recover once space frees         #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_enospc_exhaustion_degrades_cleanly_then_recovers(
    tmp_path: Path,
) -> None:
    """A full volume is a caching outage, not a serving outage (runbook §4).

    While the volume is full: put() degrades to False, leaves zero on-disk
    residue (no unit, no stray .tmp), and registers no phantom bytes.  Once
    space frees (the §6 cleanup), writes succeed again without a restart.
    """
    import errno

    volume = tmp_path / "live"
    cache = _cache(volume)
    await cache.startup_scan()
    enospc = OSError(errno.ENOSPC, "No space left on device")

    with patch.object(cache, "_atomic_write_locked", side_effect=enospc):
        assert await _put(cache, VIDEO_ID, "en", "unsaved") is False

    # no residue: the atomic-write helper unlinked its .tmp files, and the
    # failed unit left neither files nor counter debt behind
    assert list(volume.glob("*")) == []
    assert cache.unit_count == 0
    assert cache.total_bytes == 0

    # space freed (operator ran the §6 cleanup) — recovery needs no restart
    assert await _put(cache, VIDEO_ID, "en", "saved after cleanup") is True
    txt_name, json_name = _unit_stems(VIDEO_ID, "en")
    assert (volume / txt_name).read_text(encoding="utf-8") == "saved after cleanup"
    assert (volume / json_name).exists()
    assert list(volume.glob("*.tmp")) == []
    hit = await cache.get(VIDEO_ID, "en")
    assert hit is not None and hit.text == "saved after cleanup"


# --------------------------------------------------------------------------- #
# Lockfile contract: never backed up, inert if restored                        #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_backup_glob_excludes_lockfile_and_stale_lockfile_is_inert(
    tmp_path: Path,
) -> None:
    """The §2 backup/restore contract around .ytt-singleton.lock.

    The documented backup glob (*.txt + *.json) must not carry the lockfile,
    and a stale lockfile that *does* land on a restored volume (operator
    untarred carelessly) must be ignored by the scan — invisible, untouched,
    and harmless.
    """
    volume = tmp_path / "live"
    _write_unit(volume, VIDEO_ID, "en", text="data")
    lock = volume / ".ytt-singleton.lock"
    lock.write_text("stale holder record", encoding="utf-8")
    (volume / f"{VIDEO_ID}.en.txt.tmp").write_bytes(b"crash residue")

    copied = {p.name for p in _backup_volume(volume, tmp_path / "backup")}
    assert f"{VIDEO_ID}.en.txt" in copied and f"{VIDEO_ID}.en.json" in copied
    assert ".ytt-singleton.lock" not in copied   # never backed up (§1/§2)
    assert not any(name.endswith(".tmp") for name in copied)  # residue excluded

    # restoring carelessly (lockfile + residue present) is still safe:
    # the scan counts the unit, leaves the lockfile alone, cleans the residue
    cache = _cache(volume)
    await cache.startup_scan()
    assert cache.unit_count == 1
    assert cache.total_bytes > 0
    assert lock.read_text(encoding="utf-8") == "stale holder record"  # untouched
    assert list(volume.glob("*.tmp")) == []


# --------------------------------------------------------------------------- #
# Runbook ↔ manifest drift guard                                               #
# --------------------------------------------------------------------------- #


def _manifest_docs(name: str) -> list[dict[str, Any]]:
    path = MANIFEST_DIR / name
    return [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d]


def _parse_bytes(value: str) -> int:
    """Parse a k8s quantity in Mi/Gi (only shapes used by these manifests)."""
    if value.endswith("Gi"):
        return int(float(value[:-2]) * 1024 * 1024 * 1024)
    if value.endswith("Mi"):
        return int(float(value[:-2]) * 1024 * 1024)
    raise AssertionError(f"unexpected quantity {value!r} — extend the parser")


def _ytt_container(deployment: dict[str, Any]) -> dict[str, Any]:
    return next(
        c for c in deployment["spec"]["template"]["spec"]["containers"]
        if c["name"] == "ytt"
    )


def _env_map(container: dict[str, Any]) -> dict[str, str]:
    return {e["name"]: e.get("value", "") for e in container.get("env", [])}


def test_cache_runbook_matches_cache_manifests() -> None:
    """Every cache fact the runbook quotes must match the applied manifests.

    Guards the two load-bearing couplings from CACHE-RUNBOOK §4: the cache
    env/mount model, and the cap-below-volume margin that keeps the pod off
    the startup-validation CrashLoopBackOff.  If this test fails after a
    manifest change, update deploy/CACHE-RUNBOOK.md in the same commit.
    """
    pvc = _manifest_docs("pvc.yml")[0]
    assert pvc["metadata"]["name"] == "ytt-cache"
    assert pvc["spec"]["storageClassName"] == "longhorn"
    assert pvc["spec"]["accessModes"] == ["ReadWriteOnce"]
    pvc_bytes = _parse_bytes(pvc["spec"]["resources"]["requests"]["storage"])
    assert pvc_bytes == 2 * 1024**3  # the "2Gi" the runbook quotes throughout

    deployment = next(
        d for d in _manifest_docs("deployment.yml") if d.get("kind") == "Deployment"
    )
    assert deployment["metadata"]["name"] == "ytt"
    assert deployment["spec"]["replicas"] == 1  # runbook §4: one counter, one pod
    assert deployment["spec"]["strategy"]["type"] == "Recreate"  # runbook §8

    spec = deployment["spec"]["template"]["spec"]
    container = _ytt_container(deployment)
    env = _env_map(container)

    # cache volume + mount
    cache_volumes = [
        v for v in spec["volumes"] if v["name"] == "cache"
    ]
    assert len(cache_volumes) == 1
    assert cache_volumes[0]["persistentVolumeClaim"]["claimName"] == "ytt-cache"
    cache_mounts = [m for m in container["volumeMounts"] if m["name"] == "cache"]
    assert [m["mountPath"] for m in cache_mounts] == ["/cache"]

    # the env values the runbook's capacity story is built on
    assert env["YTT_CACHE_BACKEND"] == "pvc"
    assert env["YTT_CACHE_DIR"] == "/cache"
    cap_bytes = _parse_bytes(env["YTT_CACHE_MAX_BYTES"])
    # cap must stay comfortably below the nominal request: statvfs reports less
    # than nominal (ext4 reserved blocks — pvc.yml header), and startup
    # validation fails the pod when the cap clears what statvfs reports
    assert cap_bytes < pvc_bytes * 90 // 100, (
        f"YTT_CACHE_MAX_BYTES ({env['YTT_CACHE_MAX_BYTES']}) is within 10% of the "
        "PVC request — the ext4/reserved-blocks margin the startup validation "
        "relies on (pvc.yml header, CACHE-RUNBOOK §4) is gone"
    )

    # scratch facts the runbook §7 quotes
    scratch = next(v for v in spec["volumes"] if v["name"] == "scratch")
    assert scratch["emptyDir"]["sizeLimit"] == "600Mi"
    scratch_mount = next(m for m in container["volumeMounts"] if m["name"] == "scratch")
    assert scratch_mount["mountPath"] == "/scratch"
    assert env["YTT_SCRATCH_DIR"] == "/scratch"
    assert _parse_bytes(env["YTT_MAX_AUDIO_BYTES"]) < _parse_bytes("600Mi")


def test_cache_runbook_quotes_the_facts_it_depends_on() -> None:
    """The runbook must keep carrying the literals these procedures rely on.

    A doc refactor that drops e.g. the degrade event name or the lockfile rule
    would leave operators with procedures that reference nothing findable.
    """
    doc = CACHE_RUNBOOK.read_text(encoding="utf-8")
    for literal in (
        "ytt-cache",            # the PVC name (§1)
        "longhorn",             # storage class (§4)
        "/cache",               # mount path (§1)
        "1800Mi",               # the app LRU cap (§4)
        "2Gi",                  # the volume request (§4)
        "600Mi",                # scratch sizeLimit (§7)
        ".ytt-singleton.lock",  # the never-touch rule (§1/§6)
        "cache_enospc_degrade", # the degrade log event (§5)
        "cache_startup_scan",   # the post-fix re-scan signal (§3)
        "cache_miss",           # verification log pair (§9)
        "cache_write",
        "YttCacheUndersized",   # the alert that may fire (§5)
        "ytt-4f1c45c2",         # the wiring gap bead (§3/§4/§6)
        "tests/unit/test_cache_recovery.py",  # this drill (§10)
    ):
        assert literal in doc, f"CACHE-RUNBOOK.md lost the literal {literal!r}"
