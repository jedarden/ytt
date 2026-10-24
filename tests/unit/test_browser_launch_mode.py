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

Two more legs (bead ytt-8a0755bd):

- the applied manifests themselves, not just the mirror: wherever a
  declarative-config checkout exists (``$YTT_DECLARATIVE_CONFIG_DIR`` or the
  sibling ``../declarative-config`` — the same lookup
  ``tests/unit/test_deploy_parity.py`` uses), every manifest-level leg below
  also runs against ``k8s/ardenone-cluster/ytt``, so a ``run-server`` /
  ``--unsafe`` launch line regressed — or first *added* — in
  declarative-config fails here before the mirror is refreshed, and the two
  server configs are asserted equal outright.  Where no checkout exists (CI
  image builds, clean extractions) those params do not exist and the deploy/
  legs carry the guard alone.
- the note's stealth prose, tied to the server config in both directions the
  way ``TestEvidenceSpecDoc`` ties canary-gate evidence to
  ``ytt/canary_gate.py``: every stealth option the note claims must be in
  the config, and every launch option in the config must be named in the
  note (container-infra flags excepted).

Offline: reads only committed files (the declarative-config legs read a
sibling checkout when one exists).  The manifest legs need no cluster; the
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


def _declarative_config_ytt_dir() -> Path | None:
    """The applied ytt manifests, ``k8s/ardenone-cluster/ytt`` of a
    declarative-config checkout — located the same way
    ``tests/unit/test_deploy_parity.py`` locates its checkout: the env var
    first, then the sibling of this repo.  ``None`` where no checkout exists
    (CI image builds, clean extractions); a set-but-invalid env var fails
    loudly rather than skipping."""
    env_dir = os.environ.get("YTT_DECLARATIVE_CONFIG_DIR")
    if env_dir:
        root = Path(env_dir).expanduser()
        if not (root / "k8s").is_dir():
            pytest.fail(
                f"YTT_DECLARATIVE_CONFIG_DIR={env_dir!r} does not look like a "
                "declarative-config checkout (no k8s/ directory)"
            )
    else:
        root = REPO_ROOT.parent / "declarative-config"
        if not (root / "k8s").is_dir():
            return None
    ytt_dir = root.resolve() / "k8s" / "ardenone-cluster" / "ytt"
    return ytt_dir if ytt_dir.is_dir() else None


#: (label, directory) of every manifest source this guard pins: the deploy/
#: mirror always, and the applied declarative-config manifests wherever a
#: checkout exists.  The params exist only where their source does — that is
#: how the guard skips, rather than a skipif that would also hide the mirror
#: leg in a clean extraction.
_SOURCES: list[tuple[str, Path]] = [("deploy-mirror", K8S_DIR)]
_DC_YTT_DIR = _declarative_config_ytt_dir()
if _DC_YTT_DIR is not None:
    _SOURCES.append(("declarative-config", _DC_YTT_DIR))

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


def _manifest_docs(k8s_dir: Path = K8S_DIR) -> list[tuple[str, dict]]:
    docs: list[tuple[str, dict]] = []
    for path in sorted(k8s_dir.glob("*.yml")):
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


def _launch_invocations(k8s_dir: Path = K8S_DIR) -> list[tuple[str, str, str, str]]:
    """(file, container, mode, rest-of-line) for every Playwright browser
    server start in any manifest of the source directory."""
    found: list[tuple[str, str, str, str]] = []
    for fname, doc in _manifest_docs(k8s_dir):
        for cname, script in _container_scripts(doc):
            for match in _LAUNCH_RE.finditer(_executable_lines(script)):
                found.append((fname, cname, match.group(1), match.group(2)))
    return found


def _server_config(script: str) -> dict:
    """The heredoc'd ``launch-server`` config (same extraction the
    network-policy guard uses for its config leg)."""
    body = script.split("<<'JSON'\n", 1)[1].split("\nJSON", 1)[0]
    return json.loads(body)


def _browser_deployment(k8s_dir: Path = K8S_DIR) -> tuple[str, dict]:
    for fname, doc in _manifest_docs(k8s_dir):
        if (
            doc.get("kind") == "Deployment"
            and doc.get("metadata", {}).get("name") == "ytt-browser"
        ):
            return fname, doc
    pytest.fail(f"no ytt-browser Deployment found in {k8s_dir}")


def _server_config_from(k8s_dir: Path) -> dict:
    _, dep = _browser_deployment(k8s_dir)
    return _server_config(_container_scripts(dep)[0][1])


def _manifest_server_config() -> dict:
    return _server_config_from(K8S_DIR)


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
# The manifests — the authoritative launch path (deploy/ mirror + the applied
# declarative-config copy wherever a checkout exists)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "k8s_dir"), _SOURCES, ids=[s for s, _ in _SOURCES]
)
def test_the_browser_deployment_is_the_only_playwright_server(source, k8s_dir):
    found = _launch_invocations(k8s_dir)
    assert found, (
        f"no `python3 -m playwright <mode>` start found in any {source} "
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
        f"Deployment in {source}: {strangers} — a second launch site would "
        "also need the launch-server --config treatment and a leg in this "
        "guard"
    )


@pytest.mark.parametrize(
    ("source", "k8s_dir"), _SOURCES, ids=[s for s, _ in _SOURCES]
)
def test_browser_deployment_launches_via_launch_server_with_config(source, k8s_dir):
    fname, dep = _browser_deployment(k8s_dir)
    containers = _container_scripts(dep)
    assert len(containers) == 1, (
        f"{fname}: the browser Deployment grew a second container"
    )
    assert dep["spec"]["template"]["spec"]["containers"][0].get("command") == [
        "bash",
        "-c",
    ], "bash must stay PID 1 — it reaps Chromium's zombie helpers and runs the 6 h recycle loop"

    matches = list(_LAUNCH_RE.finditer(_executable_lines(containers[0][1])))
    assert len(matches) == 1, "expected exactly one browser server start"
    mode, rest = matches[0].group(1), matches[0].group(2)
    assert mode == "launch-server", (
        f"{fname} launches the browser via `{mode}` — run-server silently "
        "drops client args/ignoreDefaultArgs since Playwright 1.6x (measured "
        "39/39 empty bodies, 2026-10-07); launch via launch-server"
    )
    config = re.search(r"--config\s+(\S+)", rest)
    assert config, (
        f"{fname}: launch-server lost its --config — without a server-side "
        "config the stealth options are not fixed where clients cannot "
        "change them"
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


@pytest.mark.parametrize(
    ("source", "k8s_dir"), _SOURCES, ids=[s for s, _ in _SOURCES]
)
def test_no_launch_path_uses_run_server_or_unsafe(source, k8s_dir):
    """Swept across EVERY container of EVERY manifest in the source: the
    forbidden modes must not sneak back in via a second Deployment —
    including one that exists only in declarative-config and has not been
    mirrored yet."""
    offenders = []
    for fname, doc in _manifest_docs(k8s_dir):
        for cname, script in _container_scripts(doc):
            executable = _executable_lines(script)
            offenders += [
                (fname, cname, token)
                for token in FORBIDDEN_LAUNCH_TOKENS
                if token in executable
            ]
    assert not offenders, (
        f"{source}: {offenders}: run-server/--unsafe reached an executable "
        "launch line. run-server silently drops client args/ignoreDefaultArgs "
        "(Playwright 1.6x; 39/39 empty bodies measured 2026-10-07) and "
        "--unsafe lets any client set executablePath. Launch via "
        "`launch-server --config` with the stealth options in the config "
        "(docs/notes/browser-fetch.md)."
    )


@pytest.mark.parametrize(
    ("source", "k8s_dir"), _SOURCES, ids=[s for s, _ in _SOURCES]
)
def test_no_launch_path_names_an_executable_path(source, k8s_dir):
    for fname, doc in _manifest_docs(k8s_dir):
        for cname, script in _container_scripts(doc):
            assert "executablePath" not in _executable_lines(script), (
                f"{source} {fname}/{cname}: executablePath in the launch path "
                "pins process execution to a chosen binary — keep the "
                "browser coming from the image's own Chromium (channel=chromium)"
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


@pytest.mark.skipif(
    _DC_YTT_DIR is None,
    reason="no declarative-config checkout (checked $YTT_DECLARATIVE_CONFIG_DIR "
    "and ../declarative-config) — the applied-copy tie only runs where the "
    "applied manifests are checked out",
)
def test_declarative_config_server_config_equals_the_mirror():
    """Whole-file byte equality is the deploy-parity guard's job; this names
    the security-relevant payload — the applied manifest's launch config
    must be exactly the one the mirror (and, through the ties below, the
    compose example and the client's advisory options) pins."""
    dc_cfg = _server_config_from(_DC_YTT_DIR)
    mirror_cfg = _manifest_server_config()
    assert dc_cfg == mirror_cfg, (
        "the applied declarative-config launch config drifted from the "
        f"deploy/ mirror's:\n  applied: {dc_cfg!r}\n  mirror:  {mirror_cfg!r}\n"
        "declarative-config is the source of truth — move both together "
        "(refresh the mirror per deploy/README.md), so the stealth contract "
        "cannot diverge between what is pinned here and what runs"
    )


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
    # The guarantees the contract rests on — the same shape
    # TestEvidenceSpecDoc holds the evidence spec to: the measured failure,
    # the RCE mechanism, and the applied-manifest sweep this guard runs.
    norm = _note()
    for phrase, why in (
        ("39/39", "the measured run-server failure the contract rests on"),
        ("executablePath", "the --unsafe RCE mechanism"),
        ("declarative-config", "the applied-manifest sweep this guard runs"),
    ):
        assert phrase in norm, (
            f"docs/notes/browser-fetch.md lost the {phrase!r} guarantee "
            f"({why}) — the note, the manifest and this guard are tied; if "
            "the contract genuinely changed, move all three together"
        )


# ---------------------------------------------------------------------------
# The note's stealth section ⇄ the server config — the TestEvidenceSpecDoc tie
# ---------------------------------------------------------------------------

#: Server-config ``args`` that are container infrastructure, not stealth —
#: the note's "Stealth is load-bearing" section legitimately does not name
#: them.  Anything else the config grows must be documented there.
_NON_STEALTH_ARGS = frozenset(
    {
        "--no-sandbox",  # Chromium in a container without user namespaces
        "--disable-dev-shm-usage",  # /dev/shm is a size-capped emptyDir
    }
)


def _note() -> str:
    """The note with whitespace runs collapsed — its hard line wrapping must
    not be able to hide a phrase from the drift guard."""
    return " ".join(NOTE.read_text(encoding="utf-8").split())


def test_note_stealth_claims_are_true_of_the_server_config():
    """Note → config: every stealth option the note claims must actually be
    in the deployed server config — the direction TestEvidenceSpecDoc holds
    the evidence spec to the code (its guarantees must still be true)."""
    note = _note()
    cfg = _manifest_server_config()
    assert f"`channel={cfg['channel']}`" in note, (
        f"the note no longer claims `channel={cfg['channel']}` — the channel "
        "is the difference between full Chromium and the detected headless "
        "shell; move note and config together"
    )
    assert "new headless" in note, "the note no longer claims new headless"
    assert cfg["headless"] is True, (
        "the note claims new headless but the server config disables it — "
        "move note and config together"
    )
    assert "`--enable-automation` removed" in note, (
        "the note no longer records that --enable-automation is removed"
    )
    assert "--disable-blink-features=AutomationControlled" in note, (
        "the note no longer names "
        "--disable-blink-features=AutomationControlled"
    )
    # the init script the note describes is client-side, injected per context
    assert "navigator.webdriver" in note
    assert "webdriver" in browser_fetch.STEALTH_INIT_JS, (
        "the note describes an init script hiding navigator.webdriver, but "
        "browser_fetch.STEALTH_INIT_JS no longer touches webdriver — the "
        "third stealth leg is gone; move note and client together"
    )


def test_every_server_config_launch_option_is_documented():
    """Config → note: the other TestEvidenceSpecDoc direction (its code
    surface must be documented) — a launch option in the deployed server
    config that the note does not record is undocumented drift, so whoever
    changes the config must change the note with it."""
    note = _note()
    cfg = _manifest_server_config()
    undocumented = [
        flag for flag in cfg.get("ignoreDefaultArgs", []) if flag not in note
    ]
    undocumented += [
        flag
        for flag in cfg.get("args", [])
        if flag not in note and flag not in _NON_STEALTH_ARGS
    ]
    assert not undocumented, (
        "server-config launch options absent from docs/notes/browser-fetch.md: "
        f"{undocumented} — the note's 'Stealth is load-bearing' section is "
        "the recorded rationale for every launch option; document the new "
        "flag there (or, if it is container infrastructure rather than "
        "stealth, name it in _NON_STEALTH_ARGS here)"
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
