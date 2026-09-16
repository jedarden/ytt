"""Manifest-level assertions for the single-replica invariant (plan §Design
constraints: "Single replica (v1) … correct only at ``replicas: 1``").

Every Deployment under ``deploy/k8s/`` must pin ``replicas: 1`` **explicitly**
and use ``strategy: Recreate`` — a default RollingUpdate with ``maxSurge >= 1``
briefly runs two pods against the split in-process state (cache byte-counter,
single-flight map, Whisper job registry), which is exactly the bug class the
invariant exists to prevent.  Asserted here so a future manifest edit cannot
silently reintroduce scale-out or a rolling strategy; if a future Deployment
legitimately supports N>1, this test is the place to make that decision
explicit (see ``docs/notes/single-replica.md`` for what scale-out requires).

Also pins the coupling that makes the startup flock tripwire
(``ytt.singleton``) effective: the main Deployment must mount the
``ytt-cache`` PVC — the one shared path every replica would contend on.
"""

from __future__ import annotations

from pathlib import Path

import yaml  # via fastmcp (runtime dependency) — always present in the venv

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_ROOT = REPO_ROOT / "deploy" / "k8s"


def _k8s_yaml_documents():
    # The repository has historically used both extensions. In particular,
    # the applied ytt manifests are ``*.yml``; scanning only ``*.yaml`` would
    # make the guard vacuously pass over no Deployment documents.
    for path in sorted(DEPLOY_ROOT.rglob("*")):
        if path.suffix not in {".yml", ".yaml"}:
            continue
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if isinstance(doc, dict):
                yield path, doc


def _deployments():
    for path, doc in _k8s_yaml_documents():
        if doc.get("kind") == "Deployment":
            yield path, doc


# ---------------------------------------------------------------------------
# Every Deployment: replicas == 1, strategy Recreate
# ---------------------------------------------------------------------------


def test_deployments_exist_under_deploy_k8s():
    names = [doc["metadata"]["name"] for _, doc in _deployments()]
    assert names, (
        f"no Deployment documents found under {DEPLOY_ROOT} — the glob or "
        "repo layout changed; fix the scan before trusting these assertions"
    )


def test_every_deployment_pins_replicas_one():
    for path, doc in _deployments():
        name = doc["metadata"]["name"]
        spec = doc.get("spec", {})
        replicas = spec.get("replicas")
        assert type(replicas) is int and replicas == 1, (
            f"{path.relative_to(REPO_ROOT)}: Deployment {name!r} must pin "
            "replicas: 1 explicitly (single-replica invariant, plan §Design "
            "constraints) — omitting the field defaults to 1 today but hides "
            "the constraint"
        )


def test_every_deployment_uses_recreate_strategy():
    for path, doc in _deployments():
        name = doc["metadata"]["name"]
        strategy = doc.get("spec", {}).get("strategy", {})
        assert strategy.get("type") == "Recreate", (
            f"{path.relative_to(REPO_ROOT)}: Deployment {name!r} must use "
            "strategy: Recreate — RollingUpdate's maxSurge>=1 briefly runs "
            "two pods against split in-process state (cache byte-counter, "
            "single-flight, Whisper job registry)"
        )


# ---------------------------------------------------------------------------
# The main server Deployment: mounts the volume the tripwire locks
# ---------------------------------------------------------------------------


def test_main_deployment_mounts_cache_pvc():
    """The flock tripwire guards the cache PVC; the main Deployment must
    actually mount it, or the tripwire has nothing to contend on."""
    for _, doc in _deployments():
        if doc["metadata"]["name"] != "ytt":
            continue
        volumes = doc["spec"]["template"]["spec"].get("volumes", [])
        claims = {
            v["persistentVolumeClaim"]["claimName"]
            for v in volumes
            if "persistentVolumeClaim" in v
        }
        assert "ytt-cache" in claims, (
            "deploy/ytt must mount the ytt-cache PVC — it is the shared path "
            "ytt.singleton locks, and the only path every replica of this "
            "Deployment shares"
        )
        return
    raise AssertionError("Deployment 'ytt' not found under deploy/k8s/")
