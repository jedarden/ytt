"""The anonymous container-image pull release gate (bead ytt-7656c3a2).

``ytt-build`` (``deploy/k8s/iad-ci/argo-workflows/ytt-build.yaml``, mirrored
byte-for-byte from declarative-config and guarded by
``test_deploy_parity.py``) publishes ``ronaldraygun/ytt:<version>`` — the one
tag the README quick start and the self-hosting compose example pin (enforced
elsewhere by ``scripts/definition-of-done.sh`` and
``test_config_docs_drift.py``). Nothing used to verify what a self-hoster
experiences next: ``docker run ronaldraygun/ytt:<version>`` with no Docker
Hub login. The Hub repo was pushed private on 2026-09-16 and anonymous pulls
401'd the whole time it stayed that way — a state no CI step would notice,
yet one that breaks the documented quick start for every user.

The gate: after ``docker-build``, two further steps run against the tag CI
just pushed —

1. ``anonymous-pull-gate`` — an unauthenticated registry manifest lookup and
   a full anonymous blob pull (skopeo ``--no-creds``/``--src-no-creds``),
   the pulled manifest checked against the advertised sha256, and the tested
   tag + digest recorded as workflow output parameters. A registry
   authorization error fails the release; rate limits and outages are
   classified as such so an outage is not misread as a visibility
   regression.
2. ``quick-start-smoke`` — the published image runs as a pod sidecar with
   the documented quick-start environment and must answer ``/ytt/health``
   with ``{"status":"ok"}`` and reject an anonymous GET on the MCP mount
   with the documented ``401`` + Bearer ``WWW-Authenticate`` challenge.

This module pins that shape. The workflow manifest is hand-maintained and
nothing else keeps these properties true:

* **Anonymity is structural** — the gate pods run as the bare
  ``ytt-pull-gate`` ServiceAccount. The workflow-level ``argo-workflow``
  account injects ``imagePullSecrets`` (``docker-hub-registry``,
  ``ghcr-jedarden-registry``); a gate pod under it would authenticate its
  kubelet pull and pass against a private repo, silently voiding the gate.
  The bare account must never gain a pull secret (the manifest comment says
  the same thing).
* **Authorization errors must fail the release** — the gate steps carry no
  OnFailure retry and no ``continueOn``; a 401 is a red release, not a
  retried footnote.
* **The smoke is the documented quick start** — the startup-required env
  trio present, the documented no-Whisper mode, and the documented *default*
  upstream IdP (no ``YTT_OIDC_ISSUER``): the reference-Authentik discovery
  fetch is part of a self-hoster's first boot, and pointing the gate at a
  stub instead would silently stop exercising it.

Legs:

1. Gate wiring — step order (publish, then gate, then smoke), version
   threading, and workflow-level recording of the tested tag/digest.
2. Anonymity — the ServiceAccount and everything the gate templates must
   NOT inherit or mount.
3. Failure semantics — credential-free client flags, the authorization-error
   classification, the digest integrity check, no retry-on-failure shape.
4. Quick-start shape — the sidecar image ref and its environment.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_FILE = (
    REPO_ROOT / "deploy" / "k8s" / "iad-ci" / "argo-workflows" / "ytt-build.yaml"
)

#: The image the build publishes — and therefore the only tag the gate may
#: test ({{inputs.parameters.version}} is threaded from resolve-version, i.e.
#: from VERSION, which the release-metadata drift guard pins to the two
#: markdown docs).
PUBLISHED_TAG = "ronaldraygun/ytt:{{inputs.parameters.version}}"

#: The startup-required trio (self-hosting.md "Step 5"; Settings fails
#: closed without them — pinned at runtime by tests/image/test_image_smoke.py
#: against the built image).
REQUIRED_QUICK_START_ENV = (
    "YTT_PUBLIC_URL",
    "YTT_OAUTH_CLIENT_ID",
    "YTT_OAUTH_CLIENT_SECRET",
)

GATE_TEMPLATES = ("anonymous-pull-gate", "quick-start-smoke")


# ---------------------------------------------------------------------------
# Manifest helpers
# ---------------------------------------------------------------------------


def _documents() -> list[dict]:
    docs = [
        doc
        for doc in yaml.safe_load_all(WORKFLOW_FILE.read_text(encoding="utf-8"))
        if isinstance(doc, dict)
    ]
    assert docs, f"{WORKFLOW_FILE} parses to no YAML documents"
    return docs


def _workflow_template() -> dict:
    templates = [d for d in _documents() if d.get("kind") == "WorkflowTemplate"]
    assert len(templates) == 1, "expected exactly one WorkflowTemplate document"
    return templates[0]


def _service_accounts() -> dict[str, dict]:
    return {
        d["metadata"]["name"]: d
        for d in _documents()
        if d.get("kind") == "ServiceAccount"
    }


def _template(names: tuple[str, ...]) -> dict:
    for template in _workflow_template()["spec"]["templates"]:
        if template.get("name") in names:
            return template
    pytest.fail(f"no template named any of {names} in {_workflow_file_name()}")


def _workflow_file_name() -> str:
    return WORKFLOW_FILE.name


def _script_source(template_name: str) -> str:
    template = _template((template_name,))
    script = template.get("script") or {}
    source = script.get("source")
    assert source, f"template {template_name} has no script source"
    return source


def _steps_entry(step_name: str) -> dict:
    for group in _template(("build",))["steps"]:
        for step in group:
            if step.get("name") == step_name:
                return step
    pytest.fail(f"no step named {step_name} in the build entrypoint")


# ---------------------------------------------------------------------------
# Leg 1 — gate wiring: publish, then gate, then smoke; record what was tested
# ---------------------------------------------------------------------------


def test_gate_steps_run_after_publish_in_order():
    """Step order is the release contract: the gate only ever sees a tag that
    docker-build has already published, and the smoke only boots a tag the
    anonymous pull has already fetched."""
    names = [
        step.get("name")
        for group in _template(("build",))["steps"]
        for step in group
    ]
    assert names == [
        "resolve-version",
        "docker-build",
        "anonymous-pull-gate",
        "quick-start-smoke",
    ], names


def test_gate_steps_receive_the_published_version():
    """Both gates test the version resolve-version read from VERSION — the
    same value docker-build pushes — not an independent or hardcoded ref."""
    for step_name in GATE_TEMPLATES:
        step = _steps_entry(step_name)
        values = {
            param["name"]: param.get("value")
            for param in step.get("arguments", {}).get("parameters", [])
        }
        assert values.get("version") == (
            "{{steps.resolve-version.outputs.parameters.version}}"
        ), f"{step_name} does not test the published version: {values}"


def test_workflow_records_the_tested_tag_and_digest():
    """The release record: tested-tag and tested-digest surface as workflow
    output parameters — they live on the Workflow object and outlive podGC's
    immediate pod deletion (`kubectl get workflow <name> -o jsonpath=...`)."""
    outputs = _template(("build",)).get("outputs", {}).get("parameters", [])
    by_name = {param.get("name"): param for param in outputs}
    assert by_name.get("tested-tag", {}).get("valueFrom", {}).get(
        "parameter"
    ) == "{{steps.anonymous-pull-gate.outputs.parameters.tag}}"
    assert by_name.get("tested-digest", {}).get("valueFrom", {}).get(
        "parameter"
    ) == "{{steps.anonymous-pull-gate.outputs.parameters.digest}}"


# ---------------------------------------------------------------------------
# Leg 2 — anonymity is structural
# ---------------------------------------------------------------------------


def test_gate_service_account_has_no_pull_secrets_and_no_token():
    """The bare gate account: no imagePullSecrets (a pull secret here would
    authenticate the kubelet's pull and let quick-start-smoke pass against a
    private repo — the gate's core property), no automounted token (the
    emissary executor needs none, and the account needs no RBAC)."""
    accounts = _service_accounts()
    assert "ytt-pull-gate" in accounts, (
        "the ytt-pull-gate ServiceAccount document is missing — both gate "
        "templates reference it and would fail to schedule"
    )
    account = accounts["ytt-pull-gate"]
    assert "imagePullSecrets" not in account, (
        "ytt-pull-gate carries imagePullSecrets — the quick-start pod's "
        "kubelet pull would be authenticated and the gate would pass on a "
        "private repo. Move the gate to a secretless account instead."
    )
    assert account.get("automountServiceAccountToken") is False


def test_gate_pods_run_as_the_bare_account():
    for template_name in GATE_TEMPLATES:
        template = _template((template_name,))
        assert template.get("serviceAccountName") == "ytt-pull-gate", (
            f"{template_name} does not run as ytt-pull-gate — under the "
            "workflow-level argo-workflow account the kubelet pull inherits "
            "docker-hub-registry imagePullSecrets and anonymity is lost"
        )


def test_gate_templates_mount_no_pull_credentials():
    """Neither gate may see the docker-config secret kaniko pushes with —
    skopeo would pick it up from a well-known location and the "anonymous"
    pull would be authenticated. Positive control: docker-build must keep
    mounting it, or this guard has gone vacuous."""
    for template_name in GATE_TEMPLATES:
        mounts = _template((template_name,)).get("script", {}).get(
            "volumeMounts", []
        ) or _template((template_name,)).get("container", {}).get(
            "volumeMounts", []
        )
        offenders = [m for m in mounts if m.get("name") == "docker-config"]
        assert not offenders, (
            f"{template_name} mounts the docker-config pull secret — the "
            "anonymous-pull gate must be credential-free"
        )
    build_mounts = _template(("docker-build",))["container"]["volumeMounts"]
    assert any(m.get("name") == "docker-config" for m in build_mounts), (
        "docker-build no longer mounts docker-config — update this positive "
        "control alongside whatever replaced it"
    )


# ---------------------------------------------------------------------------
# Leg 3 — failure semantics: authorization errors fail the release
# ---------------------------------------------------------------------------


def test_pull_gate_is_credential_free_and_pulls_every_blob():
    """skopeo runs with the explicit no-credentials flags (token endpoint hit
    anonymously, then manifest + config + every layer), and the pulled
    manifest is hash-checked against the advertised digest — the recorded
    digest is evidence of what was actually fetched."""
    source = _script_source("anonymous-pull-gate")
    assert "--no-creds" in source, "manifest lookup is not anonymous"
    assert "--src-no-creds" in source, "blob pull is not anonymous"
    assert "sha256sum -c" in source, (
        "the pulled manifest is never verified against the advertised digest"
    )


def test_pull_gate_classifies_authorization_errors():
    """A 401/403 is named as such in the failure output with the operator fix
    (DEPLOY-CHECKLIST §3) — a red release must not need log archaeology to
    distinguish a visibility regression from a rate limit or an outage."""
    source = _script_source("anonymous-pull-gate")
    for marker in ("unauthorized", "401", "toomanyrequests", "DEPLOY-CHECKLIST"):
        assert marker in source, f"failure output does not classify {marker!r}"
    assert "exit 1" in source


def test_gate_steps_do_not_retry_or_swallow_failures():
    """OnError retries pod-level infrastructure failures only — a container
    exit 1 (every registry authorization error) is phase Failed and is never
    retried. No step may set continueOn: a failed gate is a failed release."""
    for template_name in GATE_TEMPLATES:
        template = _template((template_name,))
        retry = template.get("retryStrategy") or {}
        assert retry.get("retryPolicy") == "OnError", (
            f"{template_name} retry policy must be OnError (infrastructure "
            "only) — OnFailure would retry an authorization error"
        )
    for group in _template(("build",))["steps"]:
        for step in group:
            assert "continueOn" not in step, (
                f"step {step.get('name')} sets continueOn — a failed gate "
                "must fail the release, not be swallowed"
            )


def test_workflow_level_deadline_covers_the_gate_steps():
    """The workflow-level backstop must exceed the sum of per-step deadlines
    x attempts, or the whole run can be killed mid-gate by the cap added for
    pre-pod hangs."""
    spec = _workflow_template()["spec"]
    deadline = spec.get("activeDeadlineSeconds")
    assert deadline, "workflow-level activeDeadlineSeconds missing"
    steps_total = 0
    for template_name in (
        "resolve-version",
        "docker-build",
        *GATE_TEMPLATES,
    ):
        template = _template((template_name,))
        attempts = int(template.get("retryStrategy", {}).get("limit", "0")) + 1
        steps_total += template.get("activeDeadlineSeconds", 0) * attempts
    assert deadline >= steps_total, (
        f"workflow deadline {deadline}s < per-step sum {steps_total}s — the "
        "cap can kill a healthy run; raise it alongside new steps"
    )


def test_third_party_gate_images_are_pinned():
    """Fleet rule: no :latest anywhere (org CLAUDE.md pins this for
    ronaldraygun/* and CI convention pins the rest)."""
    for template_name in GATE_TEMPLATES:
        template = _template((template_name,))
        images = [template["script"]["image"]]
        images += [
            sidecar["image"] for sidecar in template.get("sidecars", [])
        ]
        for image in images:
            assert not image.rstrip("/").endswith(":latest"), image
            _, _, tag = image.rpartition(":")
            assert tag, f"unpinned image ref: {image}"


# ---------------------------------------------------------------------------
# Leg 4 — the smoke IS the documented quick start
# ---------------------------------------------------------------------------


def _sidecar() -> dict:
    sidecars = _template(("quick-start-smoke",)).get("sidecars", [])
    assert len(sidecars) == 1, "expected exactly one sidecar (the ytt server)"
    return sidecars[0]


def test_sidecar_boots_the_published_tag():
    assert _sidecar()["image"] == PUBLISHED_TAG, (
        "quick-start-smoke must boot the tag docker-build just pushed, not "
        "a pinned or derived ref"
    )


def test_sidecar_carries_the_required_quick_start_configuration():
    """The startup-required trio must be present (fail-closed otherwise) and
    Whisper must be in the documented disabled mode — the env a self-hoster
    types from the README, not a bespoke harness config."""
    env = {
        entry["name"]: entry.get("value")
        for entry in _sidecar().get("env", [])
    }
    for name in REQUIRED_QUICK_START_ENV:
        assert env.get(name), (
            f"{name} missing or valueless — the server exits 1 without it, "
            "so the smoke would never test the boot at all"
        )
    assert env.get("YTT_WHISPER_URL") == "http://127.0.0.1:9", (
        "the smoke must run the documented no-Whisper mode (an unreachable "
        "endpoint disables ASR)"
    )


def test_sidecar_uses_the_documented_default_identity_provider():
    """No YTT_OIDC_ISSUER: the quick start boots against the documented
    default (the reference Authentik), and its discovery fetch is part of a
    self-hoster's first boot. A stub issuer here would make the gate green
    while the documented path rots."""
    env = {
        entry["name"]: entry.get("value")
        for entry in _sidecar().get("env", [])
    }
    assert "YTT_OIDC_ISSUER" not in env, (
        "quick-start-smoke pins YTT_OIDC_ISSUER — the gate is supposed to "
        "exercise the documented default IdP path; if the reference "
        "Authentik is gone, change the gate and its docs deliberately, not "
        "by stubbing the check"
    )


def test_smoke_asserts_health_and_the_documented_401_challenge():
    source = _script_source("quick-start-smoke")
    assert '"status"' in source and "ok" in source, (
        "the smoke never asserts /ytt/health answers {\"status\":\"ok\"}"
    )
    assert "401" in source and "WWW-Authenticate" in source, (
        "the smoke never asserts the documented 401 + Bearer challenge on "
        "the MCP mount"
    )
