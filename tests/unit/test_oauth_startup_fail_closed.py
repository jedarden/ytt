"""Startup fail-closed gates for the OAuth client credential pair (bead
ytt-62be628f) and for the BYO-IdP issuer validation (bead ytt-c4205423).

``YTT_OAUTH_CLIENT_ID`` / ``YTT_OAUTH_CLIENT_SECRET`` are startup-required
(docs/notes/auth.md): ytt must federate to a real upstream IdP and must never
fall back to unauthenticated or self-issued operation. The pair therefore has
two properties that nothing else pins end-to-end:

1. **Missing *or blank* fails closed.** Blank (empty string) is the case a
   manifest ``env`` line interpolating an unset Secret key produces — the same
   unset-interpolation failure ``YTT_PUBLIC_URL``'s validator exists for — and
   it must exit 1 exactly like a missing variable, before any route is served.
   The id gate is ``build_auth_provider``'s own check (ytt/auth.py); the
   secret has no Settings validator and is enforced where it is *consumed* —
   it keys the HS256 upstream id-token verifier, whose construction rejects a
   falsy key.
2. **No credential value in any failure output.** The error may name the
   *variable*, never echo its *value* — a CrashLooping pod's logs are the one
   place a misconfigured secret would otherwise land in plaintext.

Two legs, mirroring tests/unit/test_deployment_health_probes.py leg 3 (the
docker-based image smoke, tests/image/test_image_smoke.py, asserts the same
posture against the built image):

1. **Gate pins (in-process)** — ``ytt.auth.build_auth_provider`` raises
   ValueError for each bad pair state. A happy-path control built from the
   same canary values proves the raise comes from the gate, not from
   construction generally — and is what makes the leak assertions meaningful:
   the canaries are *acceptable* values, so a leak would be a real credential
   landing in output, not a formatting artifact.
2. **Entrypoint pins (subprocess)** — ``python -m ytt serve`` (the image
   CMD's entrypoint: ``CMD ["ytt", "serve"]``) exits 1 with no uvicorn bind
   marker in the output, and neither canary value appears anywhere.

The secret cases raise from fastmcp's token-verifier construction, whose
message is fastmcp's, not ours — they are pinned to the exit code and the
leak property only, never to that wording (same posture as the health-probe
harness and the image smoke; the message is also a known rough edge: it does
not name ``YTT_OAUTH_CLIENT_SECRET``, recorded on the bead for follow-up).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from ytt.auth import YttOIDCProvider, build_auth_provider
from ytt.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Distinctive fake credential values. They are *valid* config — the control
#: test boots a provider from them — so any appearance in a failure path's
#: output is a genuine value leak, not a side effect of the value being
#: rejected. Never replace these with a real credential.
_CANARY_CLIENT_ID = "canary-client-id-leak-probe-3f41a9"
_CANARY_CLIENT_SECRET = "canary-client-secret-leak-probe-7b29cd"

#: Both canaries at once — a leak assertion checks *neither* ever surfaces,
#: whichever half of the pair the case under test blanks or drops.
_CANARIES = (_CANARY_CLIENT_ID, _CANARY_CLIENT_SECRET)

_VALID_PUBLIC_URL = "https://ytt.example.com/ytt"


def _settings(oauth_client_id: str | None, oauth_client_secret: str | None) -> Settings:
    """Minimal valid Settings with one credential half set to *bad state*."""
    return Settings(
        public_url=_VALID_PUBLIC_URL,
        oauth_client_id=oauth_client_id,
        oauth_client_secret=oauth_client_secret,
    )


# ---------------------------------------------------------------------------
# 1. Gate pins — build_auth_provider raises; no credential value in the error
# ---------------------------------------------------------------------------


def test_valid_canary_pair_builds_provider():
    """Control: the canary pair is *valid* config — build_auth_provider
    constructs the real provider from it. Every raise below is therefore
    attributable to the credential gate alone."""
    provider = build_auth_provider(
        _settings(_CANARY_CLIENT_ID, _CANARY_CLIENT_SECRET)
    )
    assert isinstance(provider, YttOIDCProvider)


@pytest.mark.parametrize("bad_value", [None, ""], ids=["missing", "blank"])
def test_missing_or_blank_client_id_raises(bad_value: str | None):
    """The documented gate: a missing or blank client id raises the
    variable-naming ValueError (ytt/auth.py build_auth_provider)."""
    with pytest.raises(ValueError, match="YTT_OAUTH_CLIENT_ID is required"):
        build_auth_provider(_settings(bad_value, _CANARY_CLIENT_SECRET))


@pytest.mark.parametrize("bad_value", [None, ""], ids=["missing", "blank"])
def test_missing_or_blank_client_secret_raises(bad_value: str | None):
    """A missing or blank client secret raises before the provider is built —
    the secret keys the HS256 upstream id-token verifier, whose construction
    rejects a falsy key. Message wording is fastmcp's, so only the raise is
    pinned here; the CLI leg pins the exit code."""
    with pytest.raises(ValueError):
        build_auth_provider(_settings(_CANARY_CLIENT_ID, bad_value))


@pytest.mark.parametrize(
    "bad_half,bad_value",
    [
        ("id", None),
        ("id", ""),
        ("secret", None),
        ("secret", ""),
    ],
    ids=["id-missing", "id-blank", "secret-missing", "secret-blank"],
)
def test_gate_error_never_carries_a_credential_value(
    bad_half: str, bad_value: str | None
):
    """The raised error names the variable, never echoes a value.

    The *other* half of the pair stays a valid canary: if the error path ever
    started interpolating settings into messages, this catches it — a real
    deployment's failure logs would otherwise carry the working credential.
    """
    if bad_half == "id":
        settings = _settings(bad_value, _CANARY_CLIENT_SECRET)
    else:
        settings = _settings(_CANARY_CLIENT_ID, bad_value)

    with pytest.raises(ValueError) as exc_info:
        build_auth_provider(settings)

    rendered = f"{exc_info.value}\n{repr(exc_info.value)}"
    for canary in _CANARIES:
        assert canary not in rendered, (
            f"credential value {canary!r} leaked into the startup error:\n{rendered}"
        )


# ---------------------------------------------------------------------------
# 2. Entrypoint pins — `python -m ytt serve` exits 1 before serving
# ---------------------------------------------------------------------------

#: The documented minimal-valid env, with both credential values set to the
#: canaries. Each case removes (None) or blanks ("") exactly one variable, so
#: the case under test is the *only* config difference, never ambient host
#: state (conftest's setdefault test credentials are stripped with the rest).
_VALID_STARTUP_ENV = {
    "YTT_PUBLIC_URL": _VALID_PUBLIC_URL,
    "YTT_PATH_PREFIX": "/ytt/",
    "YTT_OAUTH_CLIENT_ID": _CANARY_CLIENT_ID,
    "YTT_OAUTH_CLIENT_SECRET": _CANARY_CLIENT_SECRET,
}

#: uvicorn's own bind/serve markers — if either ever appears, the server got
#: past the credential gate and served routes on a half-configured pair.
_UVICORN_SERVED_MARKERS = ("Uvicorn running", "Application startup complete")

#: (case id, env overrides — None deletes the variable, "" blanks it, expected
#: message fragment or None for "any").  The id cases pin the documented
#: variable-naming message — a self-hoster must be able to tell which variable
#: is at fault.  The secret cases' message is fastmcp's (the verifier
#: constructor's), so only the exit code is pinned there — it does not name
#: ``YTT_OAUTH_CLIENT_SECRET``, a known rough edge recorded on the bead.
#: The issuer case pins the documented startup validation of the BYO-IdP
#: surface (bead ytt-c4205423): self-hosting.md promises a malformed
#: ``YTT_OIDC_ISSUER`` (scheme, hostname, whitespace, query, fragment)
#: "refuses to boot", and ``http://`` here is the scheme leg of that table.
_FAIL_CLOSED_CASES = [
    (
        "missing-oauth-client-id",
        {"YTT_OAUTH_CLIENT_ID": None},
        "YTT_OAUTH_CLIENT_ID is required",
    ),
    ("blank-oauth-client-id", {"YTT_OAUTH_CLIENT_ID": ""}, "YTT_OAUTH_CLIENT_ID is required"),
    ("missing-oauth-client-secret", {"YTT_OAUTH_CLIENT_SECRET": None}, None),
    ("blank-oauth-client-secret", {"YTT_OAUTH_CLIENT_SECRET": ""}, None),
    (
        "malformed-oidc-issuer",
        {"YTT_OIDC_ISSUER": "http://idp.example.com/realms/ytt"},
        "YTT_OIDC_ISSUER must use https://",
    ),
]


def _startup_env(overrides: dict[str, str | None]) -> dict[str, str]:
    """The test host's env with every YTT_* stripped, then the canary-valued
    minimal-valid set with the case's overrides applied."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("YTT_")}
    env.update(_VALID_STARTUP_ENV)
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


@pytest.mark.parametrize(
    "case_id,overrides,expected_message",
    _FAIL_CLOSED_CASES,
    ids=[case[0] for case in _FAIL_CLOSED_CASES],
)
def test_serve_exits_1_before_serving(
    case_id: str,
    overrides: dict[str, str | None],
    expected_message: str | None,
):
    """`python -m ytt serve` — the image CMD's entrypoint — exits 1, never
    reaches uvicorn, prints the documented error (id cases), and its output
    carries neither credential value."""
    run = subprocess.run(
        [sys.executable, "-m", "ytt", "serve"],
        env=_startup_env(overrides),
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    output = run.stdout + run.stderr

    assert run.returncode == 1, (
        f"[{case_id}] expected exit 1 before serving, got {run.returncode}:\n"
        f"{output[-2000:]}"
    )
    if expected_message is not None:
        assert expected_message in output, (
            f"[{case_id}] exit was 1 but the documented error is absent:\n"
            f"{output[-2000:]}"
        )
    for marker in _UVICORN_SERVED_MARKERS:
        assert marker not in output, (
            f"[{case_id}] uvicorn served routes despite the bad credential pair:\n"
            f"{output[-2000:]}"
        )
    for canary in _CANARIES:
        assert canary not in output, (
            f"[{case_id}] credential value {canary!r} leaked into serve's "
            f"stdout/stderr:\n{output[-2000:]}"
        )
