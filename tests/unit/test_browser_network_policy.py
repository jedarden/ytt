"""NetworkPolicy <-> browser wiring in the deploy/ mirror (bead ytt-1e4c448b).

The incident this pins: after the 0.2.28 rollout the connector's first real
fetch failed at connect and fell back to yt-dlp (HTTP 429) because the ``ytt``
NetworkPolicy selects the prod pod and allowed egress only to DNS, Whisper and
80/443 -- not to ``ytt-browser:3001``.  The acceptance run had passed because
its pod is not selected by that policy.  k3s's embedded kube-router controller
runs on every node of this cluster, so policies ARE enforced.

Nothing at runtime tells you this: the pod is Running, the server log is
clean, and the client only logs ``cannot reach browser server``.  So the
manifests are held to the wiring statically:

- every Deployment that sets ``YTT_BROWSER_WS_URL`` is allowed *out* to the
  browser Service by every Egress policy that selects its pods;
- the browser's ingress policy admits exactly those Deployments' pods;
- the URL's host / port / path match the Service and the server's config.

Offline: reads only the committed ``deploy/k8s/ardenone-cluster/ytt`` mirror
(whose equality with declarative-config is the parity guard's job).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlparse

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
K8S_DIR = REPO_ROOT / "deploy" / "k8s" / "ardenone-cluster" / "ytt"
URL_ENV = "YTT_BROWSER_WS_URL"


def _load_all() -> list[dict]:
    docs: list[dict] = []
    for path in sorted(K8S_DIR.glob("*.yml")):
        for doc in yaml.safe_load_all(path.read_text()):
            if isinstance(doc, dict):
                doc["_file"] = path.name
                docs.append(doc)
    return docs


def _of_kind(docs: list[dict], kind: str) -> list[dict]:
    return [d for d in docs if d.get("kind") == kind]


def _selects(selector: dict | None, labels: dict[str, str]) -> bool:
    """Evaluate a Kubernetes label selector (matchLabels + matchExpressions)."""
    selector = selector or {}
    for k, v in (selector.get("matchLabels") or {}).items():
        if labels.get(k) != v:
            return False
    for expr in selector.get("matchExpressions") or []:
        key, op, values = expr["key"], expr["operator"], expr.get("values") or []
        if op == "In" and labels.get(key) not in values:
            return False
        if op == "NotIn" and labels.get(key) in values:
            return False
        if op == "Exists" and key not in labels:
            return False
        if op == "DoesNotExist" and key in labels:
            return False
    return True


def _browser_clients(docs: list[dict]) -> list[dict]:
    """Deployments whose containers set YTT_BROWSER_WS_URL."""
    clients = []
    for dep in _of_kind(docs, "Deployment"):
        for c in dep["spec"]["template"]["spec"]["containers"]:
            env = {e["name"]: e.get("value") for e in c.get("env", [])}
            if env.get(URL_ENV):
                clients.append(
                    {
                        "name": dep["metadata"]["name"],
                        "labels": dep["spec"]["template"]["metadata"]["labels"],
                        "url": urlparse(env[URL_ENV]),
                    }
                )
    return clients


@pytest.fixture(scope="module")
def docs() -> list[dict]:
    return _load_all()


@pytest.fixture(scope="module")
def clients(docs: list[dict]) -> list[dict]:
    found = _browser_clients(docs)
    assert found, f"no Deployment under {K8S_DIR} sets {URL_ENV} -- nothing to wire"
    return found


@pytest.fixture(scope="module")
def browser_service(docs: list[dict]) -> dict:
    svcs = [s for s in _of_kind(docs, "Service") if s["metadata"]["name"] == "ytt-browser"]
    assert len(svcs) == 1, "expected exactly one Service named ytt-browser"
    return svcs[0]


@pytest.fixture(scope="module")
def browser_pod_labels(docs: list[dict], browser_service: dict) -> dict[str, str]:
    """Pod labels of the Deployment the browser Service selects."""
    sel = browser_service["spec"]["selector"]
    for dep in _of_kind(docs, "Deployment"):
        labels = dep["spec"]["template"]["metadata"]["labels"]
        if all(labels.get(k) == v for k, v in sel.items()):
            return labels
    pytest.fail(f"no Deployment's pods match the ytt-browser Service selector {sel}")


class TestUrlMatchesTheServer:
    def test_host_port_and_path_match_the_service_and_server_config(
        self, clients: list[dict], browser_service: dict, docs: list[dict]
    ) -> None:
        deployment = next(
            d for d in _of_kind(docs, "Deployment") if d["metadata"]["name"] == "ytt-browser"
        )
        script = deployment["spec"]["template"]["spec"]["containers"][0]["args"][0]
        ws_path = re.search(r'"wsPath":\s*"([^"]*)"', script)
        port = re.search(r'"port":\s*(\d+)', script)
        assert ws_path and port, "launch config (wsPath/port) not found in browser-deployment.yml"
        svc_ports = {p["port"] for p in browser_service["spec"]["ports"]}
        for c in clients:
            url = c["url"]
            assert url.scheme == "ws"
            assert url.hostname.split(".")[0] == browser_service["metadata"]["name"], (
                f"{c['name']}: {URL_ENV} host {url.hostname!r} is not the ytt-browser Service"
            )
            assert url.port in svc_ports, f"{c['name']}: port {url.port} not exposed by the Service"
            assert url.port == int(port.group(1)), "URL port != the server's configured port"
            assert url.path == "/" + ws_path.group(1), (
                f"{c['name']}: URL path {url.path!r} != server wsPath /{ws_path.group(1)}"
            )


class TestEgressFromTheClients:
    def test_every_egress_policy_selecting_a_client_allows_the_browser(
        self,
        docs: list[dict],
        clients: list[dict],
        browser_service: dict,
        browser_pod_labels: dict[str, str],
    ) -> None:
        """The incident: a policy selected the ytt pod and omitted :3001."""
        port = clients[0]["url"].port
        checked = 0
        for client in clients:
            for pol in _of_kind(docs, "NetworkPolicy"):
                spec = pol["spec"]
                if "Egress" not in spec.get("policyTypes", []):
                    continue
                if not _selects(spec["podSelector"], client["labels"]):
                    continue
                checked += 1
                allowed = any(
                    # `to` with only a podSelector == same namespace
                    any(
                        "podSelector" in t
                        and "namespaceSelector" not in t
                        and _selects(t["podSelector"], browser_pod_labels)
                        for t in rule.get("to", [])
                    )
                    and any(
                        p.get("port") == port and p.get("protocol", "TCP") == "TCP"
                        for p in rule.get("ports", [])
                    )
                    for rule in spec.get("egress", [])
                )
                assert allowed, (
                    f"NetworkPolicy {pol['metadata']['name']!r} ({pol['_file']}) selects "
                    f"{client['name']} (labels {client['labels']}) and restricts egress, but "
                    f"has no rule allowing TCP {port} to the ytt-browser pods "
                    f"({browser_pod_labels}). NetworkPolicy is ENFORCED on this cluster "
                    "(k3s embedded kube-router); the browser fetch would fail at connect "
                    "and fall back to yt-dlp, which HTTP-429s (bead ytt-1e4c448b)."
                )
        # The ytt server itself IS selected by the `ytt` policy: if that stops
        # being true this test would silently check nothing.
        assert checked >= 1, "no Egress policy selects any browser client -- guard is vacuous"


class TestIngressToTheBrowser:
    @pytest.fixture(scope="class")
    def ingress_policies(self, docs: list[dict], browser_pod_labels: dict[str, str]) -> list[dict]:
        pols = [
            p
            for p in _of_kind(docs, "NetworkPolicy")
            if "Ingress" in p["spec"].get("policyTypes", [])
            and _selects(p["spec"]["podSelector"], browser_pod_labels)
        ]
        assert pols, (
            "no ingress NetworkPolicy selects the ytt-browser pods: the server is "
            "unauthenticated and can browse from the home IP"
        )
        return pols

    def test_only_the_browser_clients_are_admitted(
        self, ingress_policies: list[dict], clients: list[dict], browser_service: dict
    ) -> None:
        port = clients[0]["url"].port
        for client in clients:
            admitted = any(
                any(
                    "podSelector" in f
                    and "namespaceSelector" not in f
                    and _selects(f["podSelector"], client["labels"])
                    for f in rule.get("from", [])
                )
                and any(p.get("port") == port for p in rule.get("ports", []))
                for pol in ingress_policies
                for rule in pol["spec"].get("ingress", [])
            )
            assert admitted, f"ingress policy does not admit {client['name']} on :{port}"

    @pytest.mark.parametrize("stranger", [{"app": "ytt-canary"}, {"app": "whisper-openai"}, {}])
    def test_other_pods_are_not_admitted(
        self, ingress_policies: list[dict], clients: list[dict], stranger: dict
    ) -> None:
        port = clients[0]["url"].port
        for pol in ingress_policies:
            for rule in pol["spec"].get("ingress", []):
                for f in rule.get("from", []):
                    assert "ipBlock" not in f, "ipBlock would admit non-ytt pods"
                    assert "namespaceSelector" not in f or "podSelector" in f, (
                        "a bare namespaceSelector admits every pod in the namespace"
                    )
                    if "podSelector" in f and "namespaceSelector" not in f and any(
                        p.get("port") == port for p in rule.get("ports", [])
                    ):
                        assert not _selects(f["podSelector"], stranger), (
                            f"ingress policy admits {stranger or 'any pod'} to the browser"
                        )

    def test_ingress_policy_does_not_restrict_egress(self, ingress_policies: list[dict]) -> None:
        """Ingress-only on purpose: egress (YouTube, PyPI at start) stays open so
        this policy cannot cut the server off from the sites it exists to reach."""
        for pol in ingress_policies:
            assert pol["spec"]["policyTypes"] == ["Ingress"], pol["metadata"]["name"]
            assert "egress" not in pol["spec"]


def test_selector_helper_semantics() -> None:
    """The evaluator the guards rely on, pinned: a wrong ``_selects`` would make
    every assertion above vacuously pass."""
    sel = {"matchExpressions": [{"key": "app", "operator": "In", "values": ["a", "b"]}]}
    assert _selects(sel, {"app": "a"}) and _selects(sel, {"app": "b", "x": "y"})
    assert not _selects(sel, {"app": "c"}) and not _selects(sel, {})
    assert _selects({"matchLabels": {"app": "a"}}, {"app": "a", "z": "1"})
    assert not _selects({"matchLabels": {"app": "a"}}, {"app": "b"})
    assert _selects(None, {"anything": "goes"}) and _selects({}, {})


def test_launch_config_json_in_the_manifest_is_valid() -> None:
    """The heredoc'd server config is embedded in a shell script inside YAML; a
    stray character makes launch-server fail at pod start, not at review."""
    docs = _load_all()
    dep = next(d for d in _of_kind(docs, "Deployment") if d["metadata"]["name"] == "ytt-browser")
    script = dep["spec"]["template"]["spec"]["containers"][0]["args"][0]
    body = script.split("<<'JSON'\n", 1)[1].split("\nJSON", 1)[0]
    cfg = json.loads(body)
    assert cfg["channel"] == "chromium" and cfg["headless"] is True
    assert "--enable-automation" in cfg["ignoreDefaultArgs"]
    assert "--disable-blink-features=AutomationControlled" in cfg["args"]
