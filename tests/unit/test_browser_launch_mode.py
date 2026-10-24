"""The ytt-browser launch-mode contract: ``launch-server --config``, never
``run-server`` / ``--unsafe`` (bead ytt-4ac8116f).

``docs/notes/browser-fetch.md`` records the measured decision pinned here:

- since Playwright 1.6x, ``run-server`` silently drops client-supplied
  ``args`` / ``ignoreDefaultArgs`` — the options that stop the browser
  looking automated.  Measured 2026-10-07: through ``run-server`` the browser
  came up HeadlessChrome with ``navigator.webdriver === true``, YouTube
  rejected the player's PO token, and **39/39 in-cluster browser fetches
  returned empty bodies** (the failure that burned the first acceptance run).
- ``--unsafe`` re-enables client options — and with them ``executablePath``:
  arbitrary command execution in the ytt-browser pod, available to anything
  that can reach :3001.

So the browser is launched by ``python3 -m playwright launch-server --config``
with the stealth options fixed in the server config, and nothing in the launch
path may invoke ``run-server`` or pass ``--unsafe``.  The contract exists in
three copies that are tied here so they cannot silently diverge:

- the server config heredoc in the ``ytt-browser`` Deployment
  (``deploy/k8s/ardenone-cluster/ytt/browser-deployment.yml`` — the
  authoritative copy; byte-equality with the applied declarative-config
  manifests is the deploy-parity guard's job,
  ``tests/unit/test_deploy_parity.py``);
- the self-hosting compose example (``docs/usage/self-hosting.md``);
- the client's advisory ``ytt.browser_fetch.BROWSER_LAUNCH_OPTIONS`` (sent in
  the connection URL; honoured only by servers that accept client launch
  options — which the deployed launch-server deliberately is not).

Offline: reads only committed files.  The manifest legs need no cluster; the
client legs need no Playwright.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest
import yaml

from ytt import browser_fetch
from ytt.browser_fetch import BROWSER_LAUNCH_OPTIONS, BrowserInfraError
from ytt.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
K8S_DIR = REPO_ROOT / "deploy" / "k8s" / "ardenone-cluster" / "ytt"
SELF_HOSTING = REPO_ROOT / "docs" / "usage" / "self-hosting.md"
NOTE = REPO_ROOT / "docs" / "notes" / "browser-fetch.md"

#: Tokens that must never reach an executable launch line.  ``run-server``
#: silently drops the client stealth options (39/39 empty bodies, 2026-10-07);
#: ``--unsafe`` hands every client ``executablePath`` — arbitrary command
#: execution in the pod.
FORBIDDEN_LAUNCH_TOKENS = ("run-server", "--unsafe")

#: How a Playwright browser server may be started: one canonical invocation
#: shape per (comment-stripped) container script line.  Group 1 is the mode —
#: the whole contract hangs on it being ``launch-server``.
_LAUNCH_RE = re.compile(
    r"python3\s+-m\s+playwright\s+(launch-server|run-server)\b([^\n]*)"
)

#: Client-side calls that would acquire a browser OUTSIDE the pinned server:
#: a local launch runs a browser in the ytt pod with client-chosen (i.e.
#: unstealthed) options, CDP connect speaks a different server's protocol,
#: and an ``executablePath=`` assignment aims process execution at a chosen
#: binary.  Assignment-shaped so prose *about* the risk cannot trip it.
_FORBIDDEN_CLIENT_CALLS = (
    r"\.launch\(",
    r"launch_persistent_context\(",
    r"connect_over_cdp\(",
    r"executablePath\s*=",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _executable_lines(script: str) -> str:
    """Script minus full-line comments.

    Prose *about* the contract (the manifest header and the compose example
    both explain why ``run-server`` is forbidden) must not silence a guard on
    the launch lines themselves — hence comment stripping, not raw grep.
    """
    return "\n".join(
        line for line in script.splitlines() if not line.lstrip().startswith("#")
    )


def _manifest_docs() -> list[tuple[str, dict]]:
    docs: list[tuple[str, dict]] = []
    for path in sorted(K8S_DIR.glob("*.yml")):
        for doc in yaml.safe_load_all(path.read_text()):
            if isinstance(doc, dict):
                docs.append((path.name, doc))
    return docs


def _container_scripts(doc: dict) -> list[tuple[str, str]]:
    """(container name, command+args joined) for a Deployment or Pod doc."""
    spec = doc.get("spec") or {}
    pod_spec = spec["template"]["spec"] if "template" in spec else spec
    return [
        (
            container.get("name", "?"),
            " ".join(
                [*(container.get("command") or []), *(container.get("args") or [])]
            ),
        )
        for container in (pod_spec.get("containers") or [])
    ]


def _launch_invocations() -> list[tuple[str, str, str, str]]:
    """(file, container, mode, rest-of-line) for every Playwright browser
    server start in any mirrored manifest."""
    found: list[tuple[str, str, str, str]] = []
    for fname, doc in _manifest_docs():
        for cname, script in _container_scripts(doc):
            for match in _LAUNCH_RE.finditer(_executable_lines(script)):
                found.append((fname, cname, match.group(1), match.group(2)))
    return found


def _server_config(script: str) -> dict:
    """The heredoc'd ``launch-server`` config (same extraction the
    network-policy guard uses for its config leg)."""
    body = script.split("<<'JSON'\n", 1)[1].split("\nJSON", 1)[0]
    return json.loads(body)


def _browser_deployment() -> tuple[str, dict]:
    for fname, doc in _manifest_docs():
        if (
            doc.get("kind") == "Deployment"
            and doc.get("metadata", {}).get("name") == "ytt-browser"
        ):
            return fname, doc
    pytest.fail("no ytt-browser Deployment found in the deploy/ mirror")


def _manifest_server_config() -> dict:
    _, dep = _browser_deployment()
    return _server_config(_container_scripts(dep)[0][1])


def _compose_browser_script() -> str:
    """The ``browser`` service's bash script from the self-hosting example."""
    text = SELF_HOSTING.read_text()
    for block in re.findall(r"```[^\n]*\n(.*?)\n```", text, re.DOTALL):
        try:
            doc = yaml.safe_load(block)
        except yaml.YAMLError:
            continue
        services = doc.get("services") if isinstance(doc, dict) else None
        if not isinstance(services, dict):
            continue
        browser = services.get("browser")
        if isinstance(browser, dict) and isinstance(browser.get("command"), list):
            command = browser["command"]
            if len(command) == 3 and command[:2] == ["bash", "-c"]:
                return command[2]
    pytest.fail(
        "docs/usage/self-hosting.md no longer carries a compose `browser` "
        "service with a bash -c command — it is one of the three tied copies "
        "of the launch-mode contract; restore it (and keep this guard in sync)"
    )


# ---------------------------------------------------------------------------
# The manifest (deploy/ mirror) — the authoritative launch path
# ---------------------------------------------------------------------------


def test_the_browser_deployment_is_the_only_playwright_server_in_the_mirror():
    found = _launch_invocations()
    assert found, (
        "no `python3 -m playwright <mode>` start found in any mirrored "
        "manifest — the launch-mode sweep went vacuous (was "
        "browser-deployment.yml renamed or its launch line reshaped? this "
        "guard's _LAUNCH_RE must follow it)"
    )
    strangers = [
        (fname, cname) for fname, cname, _, _ in found
        if (fname, cname) != ("browser-deployment.yml", "browser")
    ]
    assert not strangers, (
        f"unexpected Playwright server start(s) outside the browser "
        f"Deployment: {strangers} — a second launch site would also need the "
        "launch-server --config treatment and a leg in this guard"
    )


def test_browser_deployment_launches_via_launch_server_with_config():
    _, dep = _browser_deployment()
    containers = _container_scripts(dep)
    assert len(containers) == 1, "the browser Deployment grew a second container"
    assert dep["spec"]["template"]["spec"]["containers"][0].get("command") == [
        "bash",
        "-c",
    ], "bash must stay PID 1 — it reaps Chromium's zombie helpers and runs the 6 h recycle loop"

    matches = list(_LAUNCH_RE.finditer(_executable_lines(containers[0][1])))
    assert len(matches) == 1, "expected exactly one browser server start"
    mode, rest = matches[0].group(1), matches[0].group(2)
    assert mode == "launch-server", (
        f"the browser is launched via `{mode}` — run-server silently drops "
        "client args/ignoreDefaultArgs since Playwright 1.6x (measured "
        "39/39 empty bodies, 2026-10-07); launch via launch-server"
    )
    config = re.search(r"--config\s+(\S+)", rest)
    assert config, (
        "launch-server lost its --config — without a server-side config the "
        "stealth options are not fixed where clients cannot change them"
    )
    written = re.search(r"cat\s+>\s+(\S+)\s+<<'JSON'", containers[0][1])
    assert written, "the heredoc that writes the server-side config is gone"
    assert config.group(1) == written.group(1), (
        f"launch-server reads {config.group(1)} but the script writes "
        f"{written.group(1)} — the config would silently not apply"
    )
    assert "--unsafe" not in rest, (
        "--unsafe re-enables client launch options, including executablePath "
        "— arbitrary command execution in this pod from anything that can "
        "reach :3001"
    )


def test_no_launch_path_uses_run_server_or_unsafe():
    """Swept across EVERY container of EVERY mirrored manifest: the forbidden
    modes must not sneak back in via a second Deployment."""
    offenders = []
    for fname, doc in _manifest_docs():
        for cname, script in _container_scripts(doc):
            executable = _executable_lines(script)
            offenders += [
                (fname, cname, token)
                for token in FORBIDDEN_LAUNCH_TOKENS
                if token in executable
            ]
    assert not offenders, (
        f"{offenders}: run-server/--unsafe reached an executable launch "
        "line. run-server silently drops client args/ignoreDefaultArgs "
        "(Playwright 1.6x; 39/39 empty bodies measured 2026-10-07) and "
        "--unsafe lets any client set executablePath. Launch via "
        "`launch-server --config` with the stealth options in the config "
        "(docs/notes/browser-fetch.md)."
    )


def test_no_launch_path_names_an_executable_path():
    for fname, doc in _manifest_docs():
        for cname, script in _container_scripts(doc):
            assert "executablePath" not in _executable_lines(script), (
                f"{fname}/{cname}: executablePath in the launch path pins "
                "process execution to a chosen binary — keep the browser "
                "coming from the image's own Chromium (channel=chromium)"
            )


def test_server_config_pins_the_stealth_launch_options():
    cfg = _manifest_server_config()
    assert cfg["channel"] == "chromium", (
        "full Chromium, not Playwright's default headless shell — the shell "
        "is detected as automation and the player's PO token is rejected"
    )
    assert cfg["headless"] is True, "new headless mode"
    assert "--enable-automation" in cfg["ignoreDefaultArgs"], (
        "the automation switch must be removed or navigator.webdriver shows"
    )
    for flag in (
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
    ):
        assert flag in cfg["args"], flag
    # the endpoint the Service and the clients are pinned to
    assert cfg["host"] == "0.0.0.0"
    assert cfg["port"] == 3001
    assert cfg["wsPath"] == "ytt"


# ---------------------------------------------------------------------------
# The drift ties — three copies of one contract
# ---------------------------------------------------------------------------


def test_compose_example_launches_via_launch_server_with_config():
    script = _compose_browser_script()
    matches = list(_LAUNCH_RE.finditer(_executable_lines(script)))
    assert len(matches) == 1, "expected exactly one browser server start"
    mode, rest = matches[0].group(1), matches[0].group(2)
    assert mode == "launch-server", (
        f"the self-hosting example launches via `{mode}` — it would ship "
        "self-hosters the 39/39-empty-body failure; use launch-server"
    )
    config = re.search(r"--config\s+(\S+)", rest)
    assert config, "the example's launch-server lost its --config"
    written = re.search(r"cat\s+>\s+(\S+)\s+<<'JSON'", script)
    assert written, "the example no longer writes the server config"
    assert config.group(1) == written.group(1)
    assert "--unsafe" not in rest


def test_compose_example_launch_config_matches_the_manifest():
    manifest_cfg = _manifest_server_config()
    compose_cfg = _server_config(_compose_browser_script())
    assert compose_cfg == manifest_cfg, (
        "the self-hosting compose example's launch config drifted from the "
        f"manifest's:\n  manifest: {manifest_cfg!r}\n  compose:  {compose_cfg!r}\n"
        "self-hosters must get the same stealth browser as the cluster — "
        "update both copies (and docs/notes/browser-fetch.md if the "
        "contract itself moved)"
    )


def test_client_launch_options_match_the_manifest_server_config():
    """BROWSER_LAUNCH_OPTIONS documents itself as the authoritative server
    config's launch subset — hold it to that (the note points at both)."""
    cfg = _manifest_server_config()
    drifted = {
        key: {"manifest": cfg.get(key), "client": value}
        for key, value in BROWSER_LAUNCH_OPTIONS.items()
        if cfg.get(key) != value
    }
    assert not drifted, (
        f"ytt.browser_fetch.BROWSER_LAUNCH_OPTIONS drifted from the "
        f"manifest's server config: {drifted} — the client copy is advisory "
        "only, but it must stay an exact copy of the server-side launch "
        "options; resync both (docs/notes/browser-fetch.md names the "
        "manifest authoritative)"
    )


def test_client_launch_options_never_carry_an_executable_path():
    assert "executablePath" not in BROWSER_LAUNCH_OPTIONS, (
        "ytt must never transmit an executablePath — not even as advisory "
        "launch options in the connection URL (the manifest-side guards keep "
        "the server config free of it; this keeps the client free of it too)"
    )


def test_note_pins_the_launch_mode_contract():
    note = NOTE.read_text()
    assert "`launch-server --config`, not `run-server`." in note, (
        "docs/notes/browser-fetch.md no longer states the launch-mode "
        "contract in those terms — the note, the manifest and this guard are "
        "tied; if the contract genuinely changed, move all three together"
    )
    assert "deploy/k8s/ardenone-cluster/ytt/browser-deployment.yml" in note, (
        "docs/notes/browser-fetch.md no longer names the authoritative "
        "manifest for the stealth launch options"
    )


# ---------------------------------------------------------------------------
# The client — overrides over the ws connection are never sent, never honored
# ---------------------------------------------------------------------------


def test_ytt_only_ever_connects_it_never_launches_a_browser():
    """The client side of the contract: ytt acquires a browser ONLY by
    connecting to the remote server.  A local ``launch*`` would run a browser
    inside the ytt pod with client-chosen (unstealthed) options; CDP connect
    speaks a different server's protocol; an ``executablePath=`` assignment
    aims process execution at a chosen binary."""
    hits = []
    for path in sorted((REPO_ROOT / "ytt").glob("*.py")):
        source = path.read_text()
        hits += [
            f"{path.name}: {pattern}"
            for pattern in _FORBIDDEN_CLIENT_CALLS
            if re.search(pattern, source)
        ]
    assert not hits, (
        f"{hits}: the browser path must acquire Chromium only via "
        "chromium.connect() to the pinned launch-server — a locally launched "
        "browser has no server-side stealth and silently regresses to the "
        "PO-token-rejected failure"
    )
    # the scan must not pass vacuously: the sanctioned surface is in use
    assert re.search(
        r"chromium\.connect\(",
        (REPO_ROOT / "ytt" / "browser_fetch.py").read_text(),
    ), "browser_fetch.py no longer connects via chromium.connect()?"


def test_fetch_sends_no_launch_overrides_over_the_ws_connection(monkeypatch):
    """What ytt actually hands the browser server: a URL carrying the advisory
    launch-options (which the manifest-side guards keep free of
    executablePath) and NOTHING ELSE.  ``chromium.connect()`` takes only
    connection parameters — that is precisely why a config-pinned
    ``launch-server`` cannot have its launch options overridden by a client.
    If a launch-shaped parameter ever appears in this call it would be
    silently ignored by the deployed server while quietly depending on one."""
    seen: dict = {}

    class RefusingChromium:
        async def connect(self, endpoint: str, **kwargs):
            seen["endpoint"] = endpoint
            seen["kwargs"] = kwargs
            raise RuntimeError("refused")  # end the fetch at the boundary

    class FakePW:
        chromium = RefusingChromium()

    @asynccontextmanager
    async def factory():
        yield FakePW()

    monkeypatch.setattr(browser_fetch, "_playwright_factory", lambda: factory)

    env = {
        "YTT_ALLOWED_SUBJECTS": "test-sub",
        "YTT_CACHE_BACKEND": "emptydir",
        "YTT_CACHE_DIR": "/tmp",
        "YTT_SCRATCH_DIR": "/tmp",
        "YTT_BROWSER_WS_URL": "ws://ytt-browser.test.local:3001/",
        "YTT_BROWSER_TIMEOUT_SEC": "5",
    }
    with patch.dict(os.environ, env, clear=False):
        settings = Settings()

    with pytest.raises(BrowserInfraError) as raised:
        asyncio.run(
            browser_fetch.browser_fetch_transcript("dQw4w9WgXcQ", None, settings)
        )
    assert raised.value.reason == "connect"

    query = parse_qs(urlparse(seen["endpoint"]).query)
    advertised = json.loads(query["launch-options"][0])
    assert "executablePath" not in advertised
    assert set(seen["kwargs"]) <= {"timeout", "slow_mo", "headers"}, (
        f"connect() was passed {sorted(seen['kwargs'])} — it takes only "
        "connection parameters; any launch option here is silently dropped "
        "by the pinned launch-server (and would signal reliance on "
        "client-chosen options, the run-server failure mode)"
    )
