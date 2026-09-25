"""Canary-gate access path — docs ↔ live RBAC drift guard (bead ytt-e78ad269).

The release-gate instructions used to tell the reader to ``kubectl exec``
through the credential-free read-only proxy (README, RUNBOOK §3 step 4,
DEPLOY-CHECKLIST §5, CANARY-MONITORING-RUNBOOK §3 step 4).  The proxy's RBAC
is ``pods``/``pods/log`` ``get|list|watch`` only — ``create`` on ``pods/exec``
is deliberately withheld (verified live 2026-09-25 through the proxy:
``auth can-i get pods|pods/log -n ytt`` → yes, ``auth can-i create pods/exec
-n ytt`` → no; an exec attempt fails with ``unable to upgrade connection:
Forbidden``) — so every one of those commands failed the moment it was
pasted.  The docs now route exec through an explicit operator kubeconfig and
give an agent a read-only corroboration path instead; these pins keep them
there:

- no doc runs ``kubectl exec`` through the credential-free proxy (``$KS
  exec``, ``kubectl --server=http://traefik… exec``) — the exact regression
  this bead fixed, swept mechanically across README + deploy/*.md
- every executable exec line carries an explicit ``--kubeconfig=`` and execs
  a container the manifests actually name
- RUNBOOK §3 keeps the gate as an operator step next to the read-only
  corroboration block, and every ``$KS`` verb stays inside the documented
  read-only set {get, describe, logs} — the verbs the proxy grants
- the corroboration commands grep real code surfaces (the startup-egress
  log line, the egress gauge), so a rename fails here and not in an
  incident
- the boundary literals (the auth can-i claims, the Forbidden error string)
  stay quoted wherever a procedure leans on them

Drift-guard legs follow the repo's docs-pin pattern (``test_deletion_runbook``,
``test_cache_recovery``): the docs quote live-RBAC facts, code log strings
and manifest names, so a change fails here until the docs follow.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml  # via fastmcp (runtime dependency) — always present in the venv

REPO_ROOT = Path(__file__).resolve().parents[2]
README = REPO_ROOT / "README.md"
RUNBOOK = REPO_ROOT / "deploy" / "RUNBOOK.md"
CHECKLIST = REPO_ROOT / "deploy" / "DEPLOY-CHECKLIST.md"
CANARY_RUNBOOK = REPO_ROOT / "deploy" / "CANARY-MONITORING-RUNBOOK.md"
MANIFEST_DIR = REPO_ROOT / "deploy" / "k8s" / "ardenone-cluster" / "ytt"

#: Every doc a gate instruction (or any exec recipe) lives in — the
#: no-exec-through-proxy invariant sweeps all of them.
DOC_PATHS = (
    README,
    RUNBOOK,
    CHECKLIST,
    CANARY_RUNBOOK,
    REPO_ROOT / "deploy" / "CACHE-RUNBOOK.md",
    REPO_ROOT / "deploy" / "OAUTH-STATE-RUNBOOK.md",
    REPO_ROOT / "deploy" / "TRANSCRIPT-DELETION-RUNBOOK.md",
)

#: A line that *runs* kubectl exec — a fenced command, not prose mentioning
#: exec (prose lines don't start with the command word).
EXEC_LINE = re.compile(r"^\s*(?:\$KS\b|kubectl\b).*\bexec\b")

#: ``deploy/<name> -c <container>`` — the docs exec by explicit container,
#: and the container must be one the named Deployment runs.
DEPLOY_CONTAINER = re.compile(r"deploy/([\w-]+)\s+-c\s+([\w-]+)")

#: The verbs a proxy-alias runbook may drive through the credential-free
#: proxy — the documented read-only set, matching what the proxy's RBAC
#: grants (verified live 2026-09-25: ``get`` on pods/pods/log; ``create`` on
#: pods/exec → no).
PROXY_VERBS = frozenset({"get", "describe", "logs"})


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _doc(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# The access boundary itself — swept across every doc                          #
# --------------------------------------------------------------------------- #


def test_no_doc_execs_through_the_credential_free_proxy() -> None:
    """The regression this bead fixed, mechanically: a gate/triage exec is
    never routed through the proxy alias (``$KS exec``) or the proxy
    endpoint (``kubectl --server=http://traefik… exec``); every executable
    exec line names an explicit operator ``--kubeconfig=`` instead."""
    for path in DOC_PATHS:
        for lineno, line in enumerate(_doc(path).splitlines(), 1):
            where = f"{path.name}:{lineno}"
            assert not re.search(r"\$KS\s+exec", line), (
                f"{where}: exec via $KS — the credential-free proxy cannot "
                "exec (create pods/exec → no); route through --kubeconfig="
            )
            assert not re.search(r"--server=\S+\s+exec\b", line), (
                f"{where}: exec against a --server proxy endpoint — the "
                "proxy cannot exec; route through --kubeconfig="
            )
            if EXEC_LINE.match(line):
                assert "--kubeconfig=" in line, (
                    f"{where}: executable exec line without --kubeconfig= "
                    "(the credential-free proxy cannot exec)"
                )


def test_exec_container_names_match_the_manifests() -> None:
    """Every ``deploy/<name> -c <container>`` pair in the docs execs a
    container the named Deployment actually runs — a container rename in
    the manifests must update the runbooks in the same commit."""
    containers: dict[str, set[str]] = {}
    for manifest in ("deployment.yml", "canary-deployment.yml"):
        docs = [
            d
            for d in yaml.safe_load_all(
                (MANIFEST_DIR / manifest).read_text(encoding="utf-8")
            )
            if d
        ]
        for doc in docs:
            if doc.get("kind") == "Deployment":
                containers[doc["metadata"]["name"]] = {
                    c["name"] for c in doc["spec"]["template"]["spec"]["containers"]
                }

    for path in DOC_PATHS:
        for lineno, line in enumerate(_doc(path).splitlines(), 1):
            for dep, container in DEPLOY_CONTAINER.findall(line):
                assert container in containers.get(dep, set()), (
                    f"{path.name}:{lineno}: execs -c {container!r} but "
                    f"Deployment/{dep} runs {sorted(containers.get(dep, set()))}"
                )


@pytest.mark.parametrize("path", [RUNBOOK, CANARY_RUNBOOK], ids=lambda p: p.name)
def test_proxy_alias_verbs_stay_read_only(path: Path) -> None:
    """Every ``$KS <verb>`` in the proxy-alias runbooks stays inside the
    documented read-only set — the verbs the proxy's RBAC actually grants.
    Anything outside it is an operator step and must not be written as
    ``$KS …``."""
    verbs = set(re.findall(r"\$KS\s+([a-z][\w-]*)", _doc(path)))
    assert verbs <= PROXY_VERBS, (
        f"{path.name}: $KS verbs outside the read-only set "
        f"{sorted(PROXY_VERBS)}: {sorted(verbs - PROXY_VERBS)}"
    )


# --------------------------------------------------------------------------- #
# RUNBOOK §3/§3.1/§7 — the canonical access-path narrative                     #
# --------------------------------------------------------------------------- #


class TestRunbookAccessPath:
    @classmethod
    def collapsed(cls) -> str:
        return _collapse(_doc(RUNBOOK))

    def test_section_3_declares_the_exec_exception(self) -> None:
        t = self.collapsed()
        assert "with one exception: step 4" in t
        assert (
            "`create` on `pods/exec`, and the proxy's RBAC deliberately "
            "withholds it" in t
        )
        assert "verified live 2026-09-25" in t
        assert "`auth can-i get pods|pods/log -n ytt` → `yes`" in t
        assert "`auth can-i create pods/exec -n ytt` → `no`" in t
        assert "`unable to upgrade connection: Forbidden`" in t
        assert "Step 4 is therefore an **operator** step" in t

    def test_gate_command_uses_an_operator_kubeconfig(self) -> None:
        t = self.collapsed()
        assert (
            'kubectl --kubeconfig="$KC" exec -n ytt deploy/ytt -c ytt '
            "-- ytt canary --gate" in t
        )
        assert 'tee "canary-gate-$(date -u +%Y%m%dT%H%M%SZ).json"' in t

    def test_read_only_corroboration_block_is_pinned(self) -> None:
        """The agent-side evidence path: real read-only commands that
        corroborate the egress half without pretending to be the gate."""
        t = self.collapsed()
        assert "Read-only corroboration while the operator gate is pending" in t
        assert "never substitutes for the gate" in t
        assert (
            "$KS logs -n ytt deploy/ytt --timestamps | grep -m1 "
            "'Startup egress probe'" in t
        )
        assert '"is_residential": true' in t
        assert "$KS logs -n ytt deploy/ytt-canary --timestamps | tail -5" in t
        assert (
            "curl -s https://mcp.ardenone.com/ytt/metrics | grep -E "
            "'ytt_egress_is_residential'" in t
        )

    def test_section_7_excludes_exec_from_the_read_only_set(self) -> None:
        t = self.collapsed()
        assert (
            "Always allowed (read-only): `get`, `describe`, and `logs` for "
            "diagnostics" in t
        )
        assert "**`exec` is not in that set**" in t
        assert "needs a kubeconfig that grants `pods/exec` on ns `ytt`" in t
        assert "hands the exec step to an operator" in t

    def test_corroboration_greps_real_code_surfaces(self) -> None:
        """The corroboration commands reference surfaces the code actually
        emits — the startup log line and the gauge the metrics endpoint
        serves — so a rename in code fails this pin instead of an operator
        staring at an empty grep."""
        server = _doc(REPO_ROOT / "ytt" / "server.py")
        assert '"Startup egress probe"' in server
        assert "is_residential" in server
        assert (
            "ytt_egress_is_residential"
            in _doc(REPO_ROOT / "ytt" / "observability.py")
        )


# --------------------------------------------------------------------------- #
# The other three instructions the bead called out                             #
# --------------------------------------------------------------------------- #


def test_readme_routes_the_gate_to_the_operator_path() -> None:
    t = _collapse(_doc(README))
    assert "granting `pods/exec` on the namespace" in t
    assert "`kubectl` proxy cannot exec" in t
    assert "the gate is an operator step" in t
    assert "`deploy/RUNBOOK.md` §3 and §3.1" in t
    # the old paste-and-fail instruction is gone
    assert "kubectl exec -n <ns>" not in t


def test_deploy_checklist_gate_step_names_the_boundary() -> None:
    t = _collapse(_doc(CHECKLIST))
    assert "the credential-free read-only proxy cannot exec" in t
    assert "(`auth can-i create pods/exec` → `no`; RUNBOOK §3/§7)" in t
    assert (
        'kubectl --kubeconfig="$KC" exec -n ytt deploy/ytt -c ytt '
        "-- ytt canary --gate" in t
    )
    assert "An agent without a `pods/exec` kubeconfig" in t
    assert "collects the read-only corroboration instead (RUNBOOK §3 step 4)" in t
    assert "hands this step to an operator" in t


def test_canary_monitoring_triage_splits_read_only_from_operator() -> None:
    t = _collapse(_doc(CANARY_RUNBOOK))
    assert "Steps 1–3 are read-only, all through the credential-free proxy" in t
    assert "An operator step: exec needs `create` on `pods/exec`" in t
    assert "KC=<a kubeconfig with pods/exec on ns ytt>" in t
    assert (
        'kubectl --kubeconfig="$KC" exec -n ytt deploy/ytt-canary -c ytt-canary '
        "-- ytt canary --once" in t
    )
    assert (
        "the one-shot probes are an operator action and steps 1–3 are what "
        "an agent runs" in t
    )
