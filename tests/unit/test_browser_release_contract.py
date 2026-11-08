"""Release-time guard for the ytt ↔ ytt-browser Playwright pair.

The browser server is deliberately not built from ytt's Dockerfile: it uses
the stock Microsoft Playwright image and installs the matching Python package
at startup.  That makes the client dependency, image tag, and startup pin a
three-way release contract.  Keep this guard next to the manifest/document
drift tests so a future bump cannot ship only one leg of the pair.

The deploy/ tree is a mirror of declarative-config.  When an applied checkout
is available, its browser manifests are checked as a second source as well;
the byte-for-byte mirror relationship remains the responsibility of
``tests/unit/test_deploy_parity.py``.
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"
UV_LOCK = REPO_ROOT / "uv.lock"
NOTE = REPO_ROOT / "docs" / "notes" / "browser-fetch.md"
RUNBOOK = REPO_ROOT / "deploy" / "RUNBOOK.md"
SELF_HOSTING = REPO_ROOT / "docs" / "usage" / "self-hosting.md"
DEPLOY_YTT_DIR = REPO_ROOT / "deploy" / "k8s" / "ardenone-cluster" / "ytt"

PLAYWRIGHT_IMAGE = "mcr.microsoft.com/playwright/python"
VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
IMAGE_RE = re.compile(
    rf"^{re.escape(PLAYWRIGHT_IMAGE)}:v(?P<version>\d+\.\d+\.\d+)-(?P<flavor>[A-Za-z0-9._-]+)$"
)
PACKAGE_PIN_RE = re.compile(r"\bplaywright==(?P<version>\d+\.\d+\.\d+)\b")


def _client_version() -> str:
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    pins = [
        match.group("version")
        for dependency in project["project"]["dependencies"]
        if (match := re.fullmatch(r"playwright==(?P<version>\d+\.\d+\.\d+)", dependency))
    ]
    if len(pins) != 1:
        pytest.fail(
            "pyproject.toml must carry exactly one exact playwright==X.Y.Z "
            f"client pin, found {pins!r}"
        )
    assert VERSION_RE.fullmatch(pins[0]), pins[0]
    return pins[0]


def _declarative_config_ytt_dir() -> Path | None:
    """Locate the applied manifests using the repository's parity-test rule."""
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


_DC_YTT_DIR = _declarative_config_ytt_dir()
_MANIFEST_SOURCES: list[tuple[str, Path]] = [("deploy-mirror", DEPLOY_YTT_DIR)]
if _DC_YTT_DIR is not None:
    _MANIFEST_SOURCES.append(("declarative-config", _DC_YTT_DIR))


def _manifest_containers(k8s_dir: Path):
    for path in sorted(k8s_dir.glob("*.y*ml")):
        for document in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if not isinstance(document, dict):
                continue
            pod_spec = document.get("spec", {}).get("template", {}).get("spec", {})
            for container in pod_spec.get("containers", []):
                if isinstance(container, dict):
                    yield path.name, document, container


def _browser_containers(k8s_dir: Path):
    return [
        (filename, document, container)
        for filename, document, container in _manifest_containers(k8s_dir)
        if str(container.get("image", "")).startswith(f"{PLAYWRIGHT_IMAGE}:")
    ]


def _script(container: dict) -> str:
    return " ".join(
        [
            *(str(value) for value in container.get("command", [])),
            *(str(value) for value in container.get("args", [])),
        ]
    )


def test_client_lock_and_dependency_pin_the_same_playwright_version():
    """The reproducible client wheel must agree with the project dependency."""
    client_version = _client_version()
    lock = tomllib.loads(UV_LOCK.read_text(encoding="utf-8"))
    locked = {
        package["version"]
        for package in lock.get("package", [])
        if package.get("name") == "playwright"
    }
    assert locked == {client_version}, (
        f"uv.lock pins playwright versions {sorted(locked)!r}, while "
        f"pyproject.toml pins {client_version}; regenerate uv.lock in the "
        "same release change"
    )


@pytest.mark.parametrize(
    ("source", "k8s_dir"),
    _MANIFEST_SOURCES,
    ids=[source for source, _ in _MANIFEST_SOURCES],
)
def test_browser_manifests_pin_the_client_version_and_explicit_noble_tag(
    source: str, k8s_dir: Path
):
    """Every stock Playwright server image is an exact client-version pair."""
    client_version = _client_version()
    expected_image = f"{PLAYWRIGHT_IMAGE}:v{client_version}-noble"
    browser_containers = _browser_containers(k8s_dir)
    assert browser_containers, (
        f"{source} has no {PLAYWRIGHT_IMAGE} container — the image-pin guard "
        "went vacuous"
    )

    for filename, document, container in browser_containers:
        image = container.get("image")
        image_match = IMAGE_RE.fullmatch(str(image))
        assert (
            image_match
            and image_match.group("version") == client_version
            and image_match.group("flavor") == "noble"
        ), (
            f"{source} {filename}/{document.get('metadata', {}).get('name')!r} "
            f"pins {image!r}; expected the exact paired tag {expected_image!r}. "
            "Do not use :latest, a major/minor-only tag, or an unversioned image."
        )
        pins = PACKAGE_PIN_RE.findall(_script(container))
        assert pins == [client_version], (
            f"{source} {filename}: startup must install exactly "
            f"playwright=={client_version}, found {pins!r}; the stock image "
            "does not provide the Python client package"
        )

    primary = [
        (filename, document, container)
        for filename, document, container in browser_containers
        if document.get("kind") == "Deployment"
        and document.get("metadata", {}).get("name") == "ytt-browser"
    ]
    assert len(primary) == 1, (
        f"{source} must contain exactly one ytt-browser Deployment with the "
        f"paired image, found {len(primary)}"
    )


def test_browser_release_docs_state_the_current_pair_and_guard():
    """Operator-facing docs must name the exact pair and the executable guard."""
    client_version = _client_version()
    expected_image = f"{PLAYWRIGHT_IMAGE}:v{client_version}-noble"
    note = NOTE.read_text(encoding="utf-8")
    runbook = RUNBOOK.read_text(encoding="utf-8")
    self_hosting = SELF_HOSTING.read_text(encoding="utf-8")

    assert expected_image in note, (
        f"{NOTE.relative_to(REPO_ROOT)} must state the current exact browser "
        f"image tag {expected_image}"
    )
    assert f"playwright=={client_version}" in note
    assert expected_image in self_hosting
    assert f"playwright=={client_version}" in self_hosting

    for phrase in (
        "Coordinated Playwright bump",
        "tests/unit/test_browser_release_contract.py",
        "declarative-config",
        "deploy/` mirror",
        "ytt canary --gate",
        "major.minor",
    ):
        assert phrase in runbook, (
            f"deploy/RUNBOOK.md lost the release-contract instruction {phrase!r}; "
            "the client, server image, mirror sync, drift guard, and canary "
            "gate must remain one operator procedure"
        )
