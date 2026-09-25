"""Scoped-deletion pin — the operator contract in deploy/TRANSCRIPT-DELETION-RUNBOOK.md.

Unit-level pin of the targeted transcript-cache deletion procedure (bead
``ytt-8f8356de``): every step an operator takes in that runbook leans on a
behavior or a fact that can be checked mechanically, so each one is
exercised here.  The runbook is surgical by construction — delete exactly
one video's unit(s) — so the pins are about *scope*: the deletion glob
cannot reach another video or the lockfile, IDs are case-sensitive, a
half-deleted unit degrades in exactly the documented direction, and the
server tolerates the deletion without a restart.

Scenarios (and the runbook section each protects):

- the whole-video glob matches exactly its own files  (§5.2 — one ID, and
  never the lockfile / another video / a prefix-sharing lookalike)
- video IDs are case-sensitive                        (§2/§3 — copy it exactly)
- single-language rm names both halves; siblings keep serving
                                                      (§5.2/§5.3 — one lang)
- a .txt without its sidecar still serves             (§5.3 — degraded, not broken)
- an orphan sidecar is invisible to every scan and no  (§5.3 — why both halves
  code path removes it                                     go in one command)
- external deletion under a live registry is tolerated (§5.1 — no maintenance
  without a restart; counter corrects on the next touch     window, no restart)
- URL→ID table == canonicalize()                       (§3 — the operator map)
- runbook ↔ manifest drift guard                       (§6 — volumes, mounts,
  the stateless canary, env names the verification cites)
- the runbook keeps the literals its procedures depend on (§5/§7/§9)

The drift-guard legs follow the repo's docs-pin pattern (``test_cache_recovery``,
``test_docs_env_coverage``, ``test_single_replica``): the runbook quotes
manifest values and code names, so a change fails here until the doc follows.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml  # via fastmcp (runtime dependency) — always present in the venv

from ytt.cache import TranscriptCache, _unit_stems
from ytt.canonicalize import canonicalize
from ytt.errors import BAD_URL, YttError

CAP = 8192  # bytes — room for several small units
VIDEO_ID = "aaaabbbbccc"      # 11 chars — the deletion target
VIDEO_ID_2 = "ddddeeeefff"    # 11 chars — a bystander video
LONGER_ID = "aaaabbbbcccc"    # 12 chars sharing the target's prefix — must never match
CASE_ID = "aaaabbbbccD"       # last char uppercase — a different string than lowercase

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = REPO_ROOT / "deploy" / "TRANSCRIPT-DELETION-RUNBOOK.md"
MANIFEST_DIR = REPO_ROOT / "deploy" / "k8s" / "ardenone-cluster" / "ytt"


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


def _cache(cache_dir: Path) -> TranscriptCache:
    return TranscriptCache(cache_dir, max_bytes=CAP, reconcile_sec=0)


async def _put(
    cache: TranscriptCache,
    video_id: str,
    lang: str,
    text: str = "hello",
    source: str = "caption_auto",
    segments: list[dict[str, Any]] | None = None,
) -> bool:
    return await cache.put(video_id, lang, text, segments, source, None)


# --------------------------------------------------------------------------- #
# The whole-video glob cannot over-match (runbook §5.2)                        #
# --------------------------------------------------------------------------- #


def test_whole_video_glob_matches_exactly_its_own_files(tmp_path: Path) -> None:
    """`rm -f /cache/<id>.*` expands to exactly that video's files — nothing else.

    The command's safety rests entirely on the glob: it must take every
    language, the whisper fallback, and .tmp residue of the one ID, and it
    must not reach a bystander video, a longer ID that shares the prefix, or
    `.ytt-singleton.lock`. (Python glob and the pod's `sh` glob agree on
    these shapes: the ID is [A-Za-z0-9_-], so the pattern has no escapes.)
    """
    volume = tmp_path / "cache"
    volume.mkdir()
    own = [
        f"{VIDEO_ID}.en.txt",
        f"{VIDEO_ID}.en.json",
        f"{VIDEO_ID}.whisper.txt",
        f"{VIDEO_ID}.whisper.json",
        f"{VIDEO_ID}.en.txt.tmp",   # crash residue — the glob takes it too
    ]
    bystanders = [
        f"{VIDEO_ID_2}.en.txt",
        f"{VIDEO_ID_2}.de.json",
        f"{LONGER_ID}.en.txt",      # prefix-sharing lookalike
        ".ytt-singleton.lock",      # never cache data
        "README.txt",               # a stem with no video-id shape at all
    ]
    for name in own + bystanders:
        (volume / name).write_text("x", encoding="utf-8")

    matched = sorted(p.name for p in volume.glob(f"{VIDEO_ID}.*"))
    assert matched == sorted(own)
    # and the bystander's own glob never touches the target
    other = sorted(p.name for p in volume.glob(f"{VIDEO_ID_2}.*"))
    assert all(name.startswith(VIDEO_ID_2 + ".") for name in other)
    assert not any(name.startswith(VIDEO_ID) for name in other)


@pytest.mark.asyncio
async def test_video_ids_are_case_sensitive(tmp_path: Path) -> None:
    """Lowercasing the ID silently deletes nothing — the runbook says copy it exactly.

    The glob check is the operator-facing half (a lowercased pattern matches
    nothing, exactly like the mis-quoted-glob trap in §9); the get check is
    the code half (cache keys are the raw 11-char string).
    """
    cache = _cache(tmp_path)
    await _put(cache, CASE_ID, "en", text="case matters")

    assert sorted(p.name for p in tmp_path.glob(f"{CASE_ID.lower()}.*")) == []
    assert await cache.get(CASE_ID.lower(), "en") is None  # miss, not the unit
    hit = await cache.get(CASE_ID, "en")
    assert hit is not None and hit.text == "case matters"


# --------------------------------------------------------------------------- #
# Half-unit and sibling contracts (runbook §5.2/§5.3)                          #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_single_language_deletion_leaves_siblings_serving(tmp_path: Path) -> None:
    """`rm -f <id>.<lang>.txt <id>.<lang>.json` takes one language; the rest serve.

    `_unit_stems` must produce exactly the two names the runbook says to
    name; deleting them must remove that language (with the registry entry
    and its bytes dropped on the next touch — the ENOENT path), leave the
    other language byte-identical, and leave the whisper fallback answering
    languages that were never cached.
    """
    cache = _cache(tmp_path)
    await _put(cache, VIDEO_ID, "de", text="german body")
    de_size = cache.total_bytes  # the counter holds exactly the de unit here
    await _put(cache, VIDEO_ID, "en", text="english body")
    await _put(cache, VIDEO_ID, "whisper", text="asr body")
    total_before = cache.total_bytes

    txt_name, json_name = _unit_stems(VIDEO_ID, "de")
    assert (txt_name, json_name) == (f"{VIDEO_ID}.de.txt", f"{VIDEO_ID}.de.json")
    (tmp_path / txt_name).unlink()
    (tmp_path / json_name).unlink()

    # the de unit itself is gone (its entry deregistered, bytes corrected on
    # this touch) — the whisper fallback is what answers the request
    fallback = await cache.get(VIDEO_ID, "de")
    assert fallback is not None and fallback.lang == "whisper"
    assert fallback.text == "asr body"
    assert cache.total_bytes == total_before - de_size
    assert cache.unit_count == 2

    hit = await cache.get(VIDEO_ID, "en")
    assert hit is not None and hit.text == "english body"
    other = await cache.get(VIDEO_ID, "fr")  # any lang falls back to whisper
    assert other is not None and other.lang == "whisper"
    assert other.text == "asr body"


@pytest.mark.asyncio
async def test_txt_without_sidecar_still_serves(tmp_path: Path) -> None:
    """A `.txt` whose `.json` is gone serves with source defaulting to caption_auto.

    Degraded, not broken — the runbook's reason the one-language command
    names both files *explicitly* rather than "the txt is enough".
    """
    cache = _cache(tmp_path)
    await _put(cache, VIDEO_ID, "en", text="body", source="caption_manual")
    (tmp_path / f"{VIDEO_ID}.en.json").unlink()

    hit = await cache.get(VIDEO_ID, "en")
    assert hit is not None
    assert hit.text == "body"
    assert hit.source == "caption_auto"  # the documented default


@pytest.mark.asyncio
async def test_orphan_sidecar_is_invisible_and_never_cleaned(tmp_path: Path) -> None:
    """A `.json` without its `.txt` is served by nothing and removed by nothing.

    The runbook's core "both halves, one command" argument: no scan
    discovers it (units are discovered by their `.txt`), no lookup serves
    it, reconcile() deregisters the dead unit but unlinks no file, and the
    byte counter never sees it. If code ever starts cleaning this, the
    runbook §5.3 paragraph is stale — this test is that tripwire.
    """
    cache = _cache(tmp_path)
    await _put(cache, VIDEO_ID, "en", text="body")
    (tmp_path / f"{VIDEO_ID}.en.txt").unlink()

    await cache.reconcile()  # the registered unit's txt is gone

    assert await cache.get(VIDEO_ID, "en") is None
    assert sorted(p.name for p in tmp_path.glob("*.txt")) == []
    # the sidecar is still on disk after both reconcile and a full re-scan
    assert (tmp_path / f"{VIDEO_ID}.en.json").exists()
    await cache.startup_scan()
    assert cache.unit_count == 0
    assert cache.total_bytes == 0
    assert (tmp_path / f"{VIDEO_ID}.en.json").exists()


# --------------------------------------------------------------------------- #
# External deletion under a live registry (runbook §5.1)                       #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_external_deletion_needs_no_restart(tmp_path: Path) -> None:
    """rm under a live cache: target misses and self-corrects, bystander untouched.

    The §5.1 claim that no quiesce/maintenance-window/restart is needed: the
    deleted unit's next lookup is an ordinary ENOENT miss that deregisters
    it and corrects the counter; every other unit keeps serving
    byte-identical text; nothing raises.
    """
    cache = _cache(tmp_path)
    await _put(cache, VIDEO_ID, "en", text="target body")
    target_size = cache.total_bytes  # the counter holds exactly one unit here
    await _put(cache, VIDEO_ID_2, "en", text="bystander body")
    total_after_seed = cache.total_bytes
    assert cache.unit_count == 2

    # the operator's whole-video rm, in its exact glob shape
    for path in tmp_path.glob(f"{VIDEO_ID}.*"):
        path.unlink()

    assert await cache.get(VIDEO_ID, "en") is None       # miss, no exception
    assert cache.unit_count == 1                          # deregistered
    assert cache.total_bytes == total_after_seed - target_size

    hit = await cache.get(VIDEO_ID_2, "en")               # bystander unharmed
    assert hit is not None and hit.text == "bystander body"
    assert cache.total_bytes == total_after_seed - target_size  # touch is not loss


# --------------------------------------------------------------------------- #
# URL → ID table (runbook §3)                                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/live/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/v/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("dQw4w9WgXcQ", "dQw4w9WgXcQ"),  # bare id — passthrough, case preserved
        ("DQW4W9WGXCQ", "DQW4W9WGXCQ"),  # lowercasing is an operator bug
    ],
)
def test_url_to_id_table_is_canonicalize(url: str, expected: str) -> None:
    """Every §3 table row must be canonicalize() itself — the doc can't drift."""
    assert canonicalize(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/playlist?list=PL123",
        "https://www.youtube.com/@somehandle",
        "https://www.youtube.com/results?search_query=x",
    ],
)
def test_uncacheable_urls_have_no_key(url: str) -> None:
    """Rejected forms can never be cached — there is no second key shape (§3)."""
    with pytest.raises(YttError) as excinfo:
        canonicalize(url)
    assert excinfo.value.error_code == BAD_URL


# --------------------------------------------------------------------------- #
# Runbook ↔ manifest drift guard (runbook §6)                                  #
# --------------------------------------------------------------------------- #


def _manifest_docs(name: str) -> list[dict[str, Any]]:
    path = MANIFEST_DIR / name
    return [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d]


def _container(deployment: dict[str, Any], name: str) -> dict[str, Any]:
    return next(
        c for c in deployment["spec"]["template"]["spec"]["containers"]
        if c["name"] == name
    )


def _env_map(container: dict[str, Any]) -> dict[str, str]:
    return {e["name"]: e.get("value", "") for e in container.get("env", [])}


def test_deletion_runbook_matches_manifests() -> None:
    """The §6 blast-radius table quotes the deployment; the deployment can't drift.

    If this fails after a manifest change, update
    deploy/TRANSCRIPT-DELETION-RUNBOOK.md in the same commit.
    """
    deployment = next(
        d for d in _manifest_docs("deployment.yml") if d.get("kind") == "Deployment"
    )
    assert deployment["metadata"]["name"] == "ytt"
    assert deployment["metadata"]["namespace"] == "ytt"  # every -n ytt in the doc
    assert deployment["spec"]["replicas"] == 1           # one writer, one reader
    assert deployment["spec"]["strategy"]["type"] == "Recreate"

    spec = deployment["spec"]["template"]["spec"]
    container = _container(deployment, "ytt")
    env = _env_map(container)

    # the three volume identities the blast-radius table separates
    volumes = {v["name"]: v for v in spec["volumes"]}
    mounts = {m["mountPath"]: m["name"] for m in container["volumeMounts"]}

    assert volumes["cache"]["persistentVolumeClaim"]["claimName"] == "ytt-cache"
    assert mounts["/cache"] == "cache"
    assert env["YTT_CACHE_DIR"] == "/cache"

    assert "emptyDir" in volumes["scratch"]  # a different volume…
    assert mounts["/scratch"] == "scratch"
    assert env["YTT_SCRATCH_DIR"] == "/scratch"  # …that the glob cannot reach

    assert volumes["oauth-state"]["persistentVolumeClaim"]["claimName"] == (
        "ytt-oauth-state"
    )
    assert mounts["/state"] == "oauth-state"
    assert env["FASTMCP_HOME"] == "/state"

    oauth_pvc = _manifest_docs("oauth-state-pvc.yml")[0]
    assert oauth_pvc["metadata"]["name"] == "ytt-oauth-state"
    assert oauth_pvc["spec"]["storageClassName"] == "longhorn"

    # the canary is stateless — the §6 row rests on the manifest having no volumes
    canary = next(
        d for d in _manifest_docs("canary-deployment.yml")
        if d.get("kind") == "Deployment"
    )
    assert canary["metadata"]["name"] == "ytt-canary"
    assert canary["spec"]["template"]["spec"].get("volumes") is None


def test_deletion_runbook_quotes_the_facts_it_depends_on() -> None:
    """The runbook must keep carrying the literals its procedures rely on.

    A doc refactor that dropped the sh -c wrapper, the lockfile rule, or the
    0.2.20 wiring-caveat bead would leave operators with procedures that
    reference nothing findable.
    """
    doc = RUNBOOK.read_text(encoding="utf-8")
    for literal in (
        "sh -c 'rm -f /cache/<id>.*'",  # the load-bearing remote-glob form (§5.2)
        ".ytt-singleton.lock",          # the never-touch rule (§2/§9)
        "ytt-cache",                    # the PVC the procedure operates on
        "ytt-oauth-state",              # the PVC it must never widen into (§6)
        "/state",
        "FASTMCP_HOME",
        "/scratch",
        "caption_auto",                 # the no-sidecar default (§5.3)
        "cache_miss",                   # the verification log trail (§7)
        "cache_write",
        "cache_hit",
        "reconcile",                    # post-fix drift correction (§7.4)
        "cache_startup_scan",
        "ytt_cache_bytes",              # the gauge §7.4 sets expectations for
        "whisper_job_status_change",    # the in-flight-job pre-flight (§5.1)
        "cursor_stale",                 # the mid-pagination client contract (§8)
        "not_found",                    # the poll path answer (§6)
        "YTT_CACHE_RECONCILE_SEC",
        "YTT_WHISPER_JOBS_PER_HOUR",
        "pods/exec",                    # the access boundary (§4)
        "ytt-4f1c45c2",                 # the wiring-gap bead the caveat cites
        "ytt-4d76a316",                 # the exec-claims correction bead
        "tests/unit/test_deletion_runbook.py",  # this pin (§10)
        "tests/unit/test_cache_recovery.py",
        "deploy/ytt-canary",
    ):
        assert literal in doc, f"TRANSCRIPT-DELETION-RUNBOOK.md lost {literal!r}"
