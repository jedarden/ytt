"""Configuration-documentation drift guard (bead ytt-7ed665bc).

The README Configuration table, ``docs/usage/configuration.md`` (the full
reference), the self-hosting guide's runnable examples, and the deployed
manifests all restate the ``ytt.config.Settings`` schema — defaults, required
variables, fail-closed behavior, path-prefix rules, proxy and OIDC
configuration, image pins.  ``tests/unit/test_docs_env_coverage.py`` already
pins that every setting *has* a documentation row; this module pins that what
the rows and examples *say* still matches the model, the validators, and the
manifests, so neither side can silently diverge.  Each drift class below has
bitten this repo before (the 0.2.15–0.2.20 half-bumped releases shipped a
stale image pin in manifests the suite never looked at; the DoD shell script
checks only the two markdown pins and does not run inside the Docker build
gate, whose test stage runs plain pytest).

Six legs:

1. **Defaults** — every documentation table row that states a literal default
   states the ``Settings`` default (byte-for-byte, so a default change in
   ``ytt/config.py`` fails here until both documents follow); semantic
   markers (``required``, ``empty = deny all``, ``= rate``, ``unset``,
   ``derived``) appear exactly on the fields whose model default is the
   matching sentinel; the README's ``*(reference …)*`` markers still point at
   the baked-in reference defaults; every ``YTT_*`` name mentioned in either
   document is a real setting (no removed/renamed setting lingers).
2. **Required variables** — the rows documented as ``required`` are exactly
   the startup-required trio, and the model still fails construction without
   ``YTT_PUBLIC_URL`` (the Settings-level leg of the trio; the OAuth pair is
   enforced at the auth layer, pinned by
   ``tests/unit/test_deployment_health_probes.py`` leg 3).
3. **Fail-closed behavior** — the validator behaviors the documents promise
   are re-proven against the real ``Settings``: ``YTT_PUBLIC_URL`` required
   with shape validation and trailing-slash normalization; the
   ``YTT_PATH_PREFIX`` slash rule; limit knobs ``>= 0`` with ``0`` = deny-all
   and the zero-rate/positive-burst rejection; Invariant 7; OIDC https/no-
   whitespace/no-query rules with the discovery URL derived exactly as
   documented and the issuer never normalized; the ``YTT_PROXY_URL``
   contract from ``docs/notes/proxy-egress.md`` (unset valid, empty an
   error, http/https only, the documented Webshare example accepted).
4. **Manifests ↔ Settings** — every ``YTT_*`` env name in a Deployment maps
   to a real Settings field, every literal env value validates (constructed
   as one merged ``Settings``), duplicate names agree across containers, and
   both ytt images pin ``ronaldraygun/ytt:<VERSION>`` exactly.
5. **Self-hosting examples ↔ Settings** — the README quick-start ``docker
   run`` flags and the self-hosting guide's compose block parse, validate as
   a ``Settings`` construction, and honor the documented public-URL/prefix
   pairing rule (``YTT_PUBLIC_URL`` ends with the ``YTT_PATH_PREFIX``
   prefix); both documents pin the release image.
6. **Canary surface** — the canary's single ``Settings`` knob
   (``YTT_CANARY_INTERVAL_SEC``) keeps a row in both documents stating the
   model default, and the compile-time knobs the ``Settings`` legs cannot
   see — the fixed probe-video ladder and the dedicated metrics port — are
   pinned across ``ytt/canary.py``, the README's canary note, the
   configuration guide's canary section and the canary Deployment/Service
   manifests (bead ytt-7ff3501a: those tunables previously lived only in an
   evidence note and the manifests, and drifted out of every table).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from ytt.canary import CANARY_VIDEO_IDS
from ytt.config import DEFAULT_OIDC_CONFIG_URL, DEFAULT_OIDC_ISSUER, Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
VERSION = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
DEPLOY_K8S = REPO_ROOT / "deploy" / "k8s"

_README = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
_GUIDE = (REPO_ROOT / "docs" / "usage" / "configuration.md").read_text(
    encoding="utf-8"
)
_SELF_HOSTING = (REPO_ROOT / "docs" / "usage" / "self-hosting.md").read_text(
    encoding="utf-8"
)

#: The documents whose Configuration tables are checked, by name (tests
#: parametrize over the name and resolve the text through this map — putting
#: the text itself in the parametrization would leak whole documents into
#: the generated test IDs).
_DOCS: dict[str, str] = {
    "README.md": _README,
    "configuration.md": _GUIDE,
}

_ENV_NAMES = {f"YTT_{name.upper()}": name for name in Settings.model_fields}

#: Settings whose documented default cell is a semantic marker rather than a
#: literal, mapped to the sentinel the model must still carry and the phrases
#: each document's row must contain.  Anything whose model default is None or
#: "" must appear here — a new Optional/required setting with neither a
#: literal default nor an entry in this map fails
#: ``test_semantic_default_markers_are_complete`` below.
_MARKER_DEFAULTS: dict[str, tuple[object, tuple[str, ...]]] = {
    "YTT_PUBLIC_URL": ("", ("required", "no fallback")),
    "YTT_ALLOWED_SUBJECTS": ("", ("empty = deny all",)),
    "YTT_RATE_LIMIT_BURST": (None, ("= rate",)),
    "YTT_OAUTH_CLIENT_ID": (None, ("required",)),
    "YTT_OAUTH_CLIENT_SECRET": (None, ("required",)),
    "YTT_OIDC_CONFIG_URL": (
        DEFAULT_OIDC_CONFIG_URL,
        # a non-empty model default documented as *derived* — its value is
        # pinned to the derivation formula by
        # test_oidc_discovery_url_is_derived_as_documented
        ("derived",),
    ),
    "YTT_JWT_SIGNING_SECRET": (None, ("unset",)),
    "YTT_PROXY_URL": (None, ("unset",)),
}

#: README rows documented only as "*(reference …)*" — the model default must
#: still BE the reference value the rest of the docs spell out literally.
_REFERENCE_MARKER_VARS = ("YTT_WHISPER_URL", "YTT_OIDC_ISSUER")


def _default_cell(doc: str, var: str) -> str | None:
    """Default cell of *var*'s table row in *doc* (``None`` if no row)."""
    m = re.search(rf"^\|\s*`{re.escape(var)}`\s*\|\s*([^|]+)\|", doc, re.MULTILINE)
    return m.group(1).strip() if m else None


def _rows(doc: str) -> list[str]:
    """Every ``YTT_*`` variable with a Configuration table row in *doc*."""
    return re.findall(r"^\|\s*`(YTT_[A-Z_]+)`\s*\|", doc, re.MULTILINE)


def _mentioned_vars(doc: str) -> list[str]:
    """Every backticked ``YTT_*`` token anywhere in *doc*."""
    return re.findall(r"`(YTT_[A-Z_]+)`", doc)


def _expected_default_literal(var: str) -> str:
    """The literal a documentation row must state for *var*."""
    return str(Settings.model_fields[_ENV_NAMES[var]].default)


# ---------------------------------------------------------------------------
# Leg 1 — defaults: documented vs model
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "var",
    sorted(
        var
        for var in _ENV_NAMES
        if var not in _MARKER_DEFAULTS and var not in _REFERENCE_MARKER_VARS
    ),
)
@pytest.mark.parametrize("doc_name", _DOCS)
def test_documented_literal_default_matches_the_model(doc_name, var):
    """A default changed in Settings must be mirrored in every stated row."""
    doc = _DOCS[doc_name]
    cell = _default_cell(doc, var)
    if cell is None:
        pytest.skip(f"{doc_name} has no row for {var} (its table is curated)")
    expected = f"`{_expected_default_literal(var)}`"
    assert expected in cell, (
        f"{doc_name} states default {cell!r} for {var}; "
        f"Settings says {expected}"
    )


def test_semantic_default_markers_are_complete():
    """The marker map covers exactly the fields without a literal default.

    A new Settings field whose default is ``None``/"" (required, optional, or
    deny-all-by-empty) must be documented with a semantic marker and added to
    ``_MARKER_DEFAULTS`` — otherwise the literal-default rule above would
    silently skip it and its documentation could drift unchecked.
    """
    sentinel_vars = {
        var
        for var, name in _ENV_NAMES.items()
        if Settings.model_fields[name].default is None
        or Settings.model_fields[name].default == ""
    }
    assert set(_MARKER_DEFAULTS) == sentinel_vars | {"YTT_OIDC_CONFIG_URL"}, (
        "_MARKER_DEFAULTS and the Settings None/''-defaulted fields disagree"
    )
    for var, (sentinel, _) in _MARKER_DEFAULTS.items():
        if var == "YTT_OIDC_CONFIG_URL":
            continue  # non-empty default; pinned to the derivation formula
        assert Settings.model_fields[_ENV_NAMES[var]].default == sentinel, (
            f"{var} is documented as {_MARKER_DEFAULTS[var]!r} but its model "
            f"default is {Settings.model_fields[_ENV_NAMES[var]].default!r}"
        )


@pytest.mark.parametrize("doc_name", _DOCS)
@pytest.mark.parametrize("var", sorted(_MARKER_DEFAULTS))
def test_documented_marker_matches_the_model(doc_name, var):
    """Semantic markers appear on exactly the fields whose model default is
    the matching sentinel — and nowhere else."""
    cell = _default_cell(_DOCS[doc_name], var)
    if cell is None:
        pytest.skip(f"{doc_name} has no row for {var} (its table is curated)")
    _, markers = _MARKER_DEFAULTS[var]
    for marker in markers:
        assert marker in cell, (
            f"{doc_name}'s {var} row should state {marker!r}; "
            f"default cell is {cell!r}"
        )


def test_readme_reference_markers_point_at_the_baked_in_defaults():
    """``*(reference …)*`` cells stay truthful about the baked-in defaults.

    The reference deployment's Whisper endpoint and Authentik issuer are
    ytt's two ardenone-specific defaults (README §Configuration); the README
    marks them prose-only, so pin the model defaults to the constants the
    other documents spell out literally.
    """
    for var in _REFERENCE_MARKER_VARS:
        cell = _default_cell(_README, var)
        assert cell is not None and "reference" in cell, (
            f"README's {var} row should keep its *(reference …)* marker; "
            f"cell is {cell!r}"
        )
    fields = Settings.model_fields
    assert fields["whisper_url"].default == _default_cell(
        _GUIDE, "YTT_WHISPER_URL"
    ).strip("`"), "model whisper_url drifted from the configuration.md literal"
    assert fields["oidc_issuer"].default == DEFAULT_OIDC_ISSUER
    assert _default_cell(_GUIDE, "YTT_OIDC_ISSUER") == f"`{DEFAULT_OIDC_ISSUER}`"


@pytest.mark.parametrize("doc_name", _DOCS)
def test_every_mentioned_setting_exists(doc_name):
    """No removed or renamed ``YTT_*`` setting lingers in the documents."""
    stale = sorted(set(_mentioned_vars(_DOCS[doc_name])) - set(_ENV_NAMES))
    assert not stale, f"{doc_name} references settings that no longer exist: {stale}"


# ---------------------------------------------------------------------------
# Leg 2 — required variables
# ---------------------------------------------------------------------------


def test_documented_required_markers_are_exactly_the_startup_trio():
    """Only the startup-required trio may claim ``required`` in the docs.

    A new startup-required setting must show up here as a docs change (and in
    the model-side list below); a setting that stops being required must lose
    its marker or this fails.
    """
    trio = {"YTT_PUBLIC_URL", "YTT_OAUTH_CLIENT_ID", "YTT_OAUTH_CLIENT_SECRET"}
    claimed = set()
    for doc in _DOCS.values():
        for var in _rows(doc):
            cell = _default_cell(doc, var)
            assert cell is not None
            if "required" in cell.lower():
                claimed.add(var)
    assert claimed == trio, (
        f"docs mark {sorted(claimed)} as required; the startup-required set "
        "is the trio (YTT_PUBLIC_URL at Settings level, the OAuth pair via "
        "ytt.auth.build_auth_provider)"
    )


def test_settings_still_require_public_url(monkeypatch):
    """The Settings-level leg of the documented required trio: with the env
    cleared, construction fails on YTT_PUBLIC_URL — there is still no
    fallback default (see the retired baked-in default's history)."""
    for var in _ENV_NAMES:
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ValidationError, match="YTT_PUBLIC_URL is required"):
        Settings()


# ---------------------------------------------------------------------------
# Leg 3 — documented fail-closed behavior, re-proven against the validators
# ---------------------------------------------------------------------------


#: The documented well-formed placeholder — stands in for the required
#: public URL in tests that exercise other settings (conftest's env default
#: is deliberately cleared so constructions are hermetic).
_TEST_PUBLIC_URL = "https://mcp.example.com/ytt"


def _build(monkeypatch, **kwargs) -> Settings:
    for var in _ENV_NAMES:
        monkeypatch.delenv(var, raising=False)
    kwargs.setdefault("public_url", _TEST_PUBLIC_URL)
    return Settings(**kwargs)


def test_public_url_fail_closed_claims(monkeypatch):
    """README/configuration.md: exits 1 if unset or empty; shape-validated
    when set; trailing slash normalized away.  (The unset leg is
    test_settings_still_require_public_url; here it is the empty/
    whitespace-only forms the documents call out.)"""
    with pytest.raises(ValidationError, match="YTT_PUBLIC_URL is required"):
        _build(monkeypatch, public_url="")
    with pytest.raises(ValidationError, match="YTT_PUBLIC_URL"):
        _build(monkeypatch, public_url="   ")
    for bad in (
        "ftp://mcp.example.com/ytt",  # non-http(s) scheme
        "https://",  # no hostname
        "https://mcp.example.com/ ytt",  # whitespace (copy-paste wrap)
        "https://mcp.example.com/ytt?a=1",  # query — audience is byte-exact
        "https://mcp.example.com/ytt#frag",  # fragment
    ):
        with pytest.raises(ValidationError, match="YTT_PUBLIC_URL"):
            _build(monkeypatch, public_url=bad)
    s = _build(monkeypatch, public_url="https://mcp.example.com/ytt/")
    assert s.public_url == "https://mcp.example.com/ytt", (
        "a trailing slash must be normalized away (RFC 8707 byte-exactness)"
    )


def test_path_prefix_slash_rule(monkeypatch):
    """configuration.md/README: YTT_PATH_PREFIX must end with '/' — startup
    exits 1 otherwise (self-hosting Step 3 calls this the most-gotten-wrong
    step); it must also start with one."""
    with pytest.raises(ValidationError, match="YTT_PATH_PREFIX"):
        _build(monkeypatch, path_prefix="ytt")
    with pytest.raises(ValidationError, match="YTT_PATH_PREFIX"):
        _build(monkeypatch, path_prefix="ytt/")
    assert _build(monkeypatch, path_prefix="/gateway/").path_prefix == "/gateway/"


@pytest.mark.parametrize(
    "field",
    ["rate_limit_per_min", "whisper_jobs_per_hour", "max_pending_whisper_jobs"],
)
def test_limit_knobs_fail_closed_on_negative_and_accept_zero(monkeypatch, field):
    """configuration.md §Authorization: each limit knob must be an integer
    >= 0 — negatives are config errors, 0 is meaningful deny-all, and there
    is no 'unlimited' escape value."""
    with pytest.raises(ValidationError, match="must be >= 0"):
        _build(monkeypatch, **{field: -1})
    assert _build(monkeypatch, **{field: 0}) is not None


def test_zero_rate_with_explicit_positive_burst_is_rejected(monkeypatch):
    """configuration.md: 0 is documented deny-all (no refill), so an explicit
    positive burst would grant a one-shot allowance contradicting it."""
    with pytest.raises(ValidationError, match="deny-all"):
        _build(monkeypatch, rate_limit_per_min=0, rate_limit_burst=5)
    s = _build(monkeypatch, rate_limit_per_min=0, rate_limit_burst=0)
    assert s.rate_limit_burst == 0  # explicit 0 capacity is valid (deny all)


def test_unset_burst_resolves_to_the_rate(monkeypatch):
    """README/configuration.md: unset burst resolves to RATE_LIMIT_PER_MIN."""
    s = _build(monkeypatch, rate_limit_per_min=7)
    assert s.rate_limit_burst == 7


def test_invariant_7_is_startup_enforced(monkeypatch):
    """configuration.md: WHISPER_TIMEOUT_SEC must exceed
    MAX_ASR_DURATION_SEC × WHISPER_REALTIME_FACTOR (Invariant 7)."""
    with pytest.raises(ValidationError, match="Invariant 7"):
        _build(
            monkeypatch,
            max_asr_duration_sec=1200,
            whisper_realtime_factor=2.0,
            whisper_timeout_sec=1200,  # 1200 × 2.0 = 2400 > 1200
        )


@pytest.mark.parametrize(
    "bad",
    [
        "http://idp.example.com/realms/ytt",  # OIDC Core §3.1.2.1: https only
        "https://idp.example.com/realms/ ytt",  # whitespace
        "https://idp.example.com/realms/ytt?a=1",  # query
        "https://idp.example.com/realms/ytt#frag",  # fragment
    ],
)
def test_oidc_issuer_shape_rules(monkeypatch, bad):
    """configuration.md: the issuer is matched byte-for-byte against the id
    token's iss claim, so whitespace/query/fragment and non-https fail
    startup instead of failing every login opaquely."""
    with pytest.raises(ValidationError, match="YTT_OIDC_ISSUER"):
        _build(monkeypatch, oidc_issuer=bad)


def test_oidc_issuer_is_never_normalized(monkeypatch):
    """configuration.md: set exactly what your IdP advertises — Authentik
    issuers end with '/', Keycloak ones do not; neither is stripped."""
    kept = _build(monkeypatch, oidc_issuer="https://idp.example.com/realms/ytt")
    assert kept.oidc_issuer == "https://idp.example.com/realms/ytt"
    slashed = _build(
        monkeypatch, oidc_issuer="https://idp.example.com/application/o/ytt/"
    )
    assert slashed.oidc_issuer == "https://idp.example.com/application/o/ytt/"


def test_oidc_discovery_url_is_derived_as_documented(monkeypatch):
    """configuration.md: unset YTT_OIDC_CONFIG_URL derives from the issuer as
    ``<issuer>/.well-known/openid-configuration`` (OIDC Discovery §4); the
    baked-in defaults satisfy the same formula."""
    s = _build(monkeypatch, oidc_issuer="https://idp.example.com/realms/ytt")
    assert (
        s.oidc_config_url
        == "https://idp.example.com/realms/ytt/.well-known/openid-configuration"
    )
    reference = _build(monkeypatch)  # no OIDC env at all
    assert reference.oidc_issuer == DEFAULT_OIDC_ISSUER
    assert reference.oidc_config_url == DEFAULT_OIDC_CONFIG_URL
    assert DEFAULT_OIDC_CONFIG_URL == (
        DEFAULT_OIDC_ISSUER.rstrip("/") + "/.well-known/openid-configuration"
    )


def test_proxy_contract_matches_proxy_egress_doc(monkeypatch):
    """docs/notes/proxy-egress.md: unset is valid direct egress; empty/
    whitespace is an error, not a silent unset; scheme must be http/https
    (no SOCKS — the httpx egress probe has none); a hostname is required;
    the guide's and self-hosting guide's example URLs are accepted."""
    assert _build(monkeypatch).proxy_url is None
    for bad in (
        "",  # empty — the manifest-interpolating-a-missing-secret case
        "   ",  # whitespace-only
        "socks5://proxy.example.com:1080",  # no SOCKS adapter
        "http://",  # no hostname
        "http://user:pass@proxy.example.com:3128 x",  # embedded whitespace
    ):
        with pytest.raises(ValidationError, match="YTT_PROXY_URL"):
            _build(monkeypatch, proxy_url=bad)
    ok = _build(
        monkeypatch, proxy_url="http://username:password@proxy.webshare.io:port"
    )
    assert ok.proxy_url == "http://username:password@proxy.webshare.io:port"


# ---------------------------------------------------------------------------
# Leg 4 — manifests ↔ Settings
# ---------------------------------------------------------------------------

#: Env vars a ytt container may carry that are not ``Settings`` fields.
#: FASTMCP_HOME points FastMCP's OAuthProxy state directory at the
#: oauth-state PVC (deployment.yml); it is FastMCP's own setting.
_MANIFEST_ENV_ALLOWLIST = frozenset({"FASTMCP_HOME"})


def _manifest_docs(kind: str):
    """Every manifest document of *kind* under deploy/k8s (same scan as
    test_single_replica — both .yml and .yaml)."""
    for path in sorted(DEPLOY_K8S.rglob("*")):
        if path.suffix not in {".yml", ".yaml"}:
            continue
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if isinstance(doc, dict) and doc.get("kind") == kind:
                yield path, doc


def _deployments():
    """Every Deployment document under deploy/k8s."""
    return _manifest_docs("Deployment")


def _container_envs(doc: dict) -> list[dict[str, str | None]]:
    envs = []
    for container in doc.get("spec", {}).get("template", {}).get("spec", {}).get(
        "containers", []
    ):
        envs.append(
            {
                entry["name"]: entry.get("value")
                for entry in container.get("env", [])
            }
        )
    return envs


def _ytt_image_containers():
    """(path, container) for every container referencing the ytt image."""
    found = []
    for path, doc in _deployments():
        for container in (
            doc.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        ):
            if str(container.get("image", "")).startswith("ronaldraygun/ytt"):
                found.append((path, container))
    return found


def test_manifest_env_names_map_to_settings_fields():
    """Every YTT_* env name a Deployment sets is a real Settings field — a
    renamed/removed setting must be renamed in the manifests in the same
    commit, not silently orphaned (extra='ignore' would otherwise drop it
    without a word)."""
    for path, doc in _deployments():
        for env in _container_envs(doc):
            unknown = set(env) - set(_ENV_NAMES) - _MANIFEST_ENV_ALLOWLIST
            assert not unknown, f"{path.name} sets unknown env vars: {sorted(unknown)}"


def test_manifest_env_values_validate_against_settings(monkeypatch):
    """The manifests' literal env values, taken together, are a valid
    Settings construction — a manifest value the model rejects (a bad path
    prefix, an unparsable size, a negative limit, a broken invariant) fails
    here instead of crash-looping the replica at deploy time."""
    merged: dict[str, str] = {}
    for path, doc in _deployments():
        for env in _container_envs(doc):
            for name, value in env.items():
                if not name.startswith("YTT_") or value is None:
                    continue  # valueFrom secrets are not literal values
                prior = merged.setdefault(name, value)
                assert prior == value, (
                    f"{name} is set to conflicting values across Deployments: "
                    f"{prior!r} vs {value!r} ({path.name})"
                )
    assert "YTT_PUBLIC_URL" in merged, (
        "the guard is vacuous unless the server Deployment sets its required "
        "public URL explicitly"
    )
    for var in _ENV_NAMES:
        monkeypatch.delenv(var, raising=False)
    settings = Settings(**{_ENV_NAMES[k]: v for k, v in merged.items()})
    assert settings.public_url == merged["YTT_PUBLIC_URL"]
    assert settings.path_prefix == merged["YTT_PATH_PREFIX"]


def test_manifests_pin_the_release_image():
    """Every ytt image ref in the manifests is exactly
    ``ronaldraygun/ytt:<VERSION>`` — no stale semver (the 0.2.15–0.2.20
    half-bumped-release class), no :latest, no bare SHA.  The DoD shell
    script checks only the two markdown pins; this is the manifest leg, and
    unlike the shell script it runs inside the Docker build gate."""
    pinned = f"ronaldraygun/ytt:{VERSION}"
    images = [
        container["image"]
        for _, container in _ytt_image_containers()
    ]
    assert images, (
        "no container under deploy/k8s references ronaldraygun/ytt — the "
        "image-pin guard is vacuous"
    )
    for image in images:
        assert image == pinned, (
            f"manifest pins {image!r}, VERSION is {VERSION} — bump the "
            "manifests in the same release commit as everything else"
        )


# ---------------------------------------------------------------------------
# Leg 5 — self-hosting examples ↔ Settings
# ---------------------------------------------------------------------------


def _fenced_block(doc: str, marker: str) -> str:
    """The fenced code block of *doc* containing *marker*."""
    for block in re.findall(r"```[a-z]*\n(.*?)```", doc, re.DOTALL):
        if marker in block:
            return block
    raise AssertionError(f"no fenced code block containing {marker!r}")


def _quick_start_env() -> dict[str, str]:
    """The README quick-start ``docker run`` -e flags."""
    block = _fenced_block(_README, "docker run")
    pairs = dict(re.findall(r"-e\s+(YTT_[A-Z_]+)=(\S+)", block))
    assert "YTT_PUBLIC_URL" in pairs and "YTT_PATH_PREFIX" in pairs, (
        "the README quick start must set the required public URL and the "
        "path prefix for the pairing rule below to be checkable"
    )
    return pairs


def _compose_env() -> dict[str, str]:
    """The self-hosting guide's compose ``YTT_*`` environment entries."""
    block = _fenced_block(_SELF_HOSTING, "services:")
    pairs = dict(re.findall(r'^\s*(YTT_[A-Z_]+):\s*"([^"]*)"', block, re.MULTILINE))
    assert "YTT_PUBLIC_URL" in pairs and "YTT_PATH_PREFIX" in pairs, (
        "the compose example must set the required public URL and the path "
        "prefix for the pairing rule below to be checkable"
    )
    return pairs


@pytest.mark.parametrize(
    "label,env",
    [("README quick start", _quick_start_env()), ("compose example", _compose_env())],
)
def test_selfhosting_examples_validate_against_settings(monkeypatch, label, env):
    """The copy-paste examples a self-hoster actually runs must construct a
    valid Settings — and honor the documented pairing rule (self-hosting
    Step 3): YTT_PUBLIC_URL ends with the YTT_PATH_PREFIX prefix."""
    unknown = set(env) - set(_ENV_NAMES)
    assert not unknown, f"{label} sets settings that no longer exist: {unknown}"
    for var in _ENV_NAMES:
        monkeypatch.delenv(var, raising=False)
    settings = Settings(**{_ENV_NAMES[k]: v for k, v in env.items()})
    prefix = settings.path_prefix
    assert settings.public_url.endswith(prefix.rstrip("/")), (
        f"{label}: YTT_PUBLIC_URL ({settings.public_url!r}) must include the "
        f"path prefix ({prefix!r}) — self-hosting Step 3"
    )


def test_compose_prefix_is_the_documented_default():
    """The compose example presents /ytt/ as *the* default prefix (Step 3);
    if the model default moves, the example must follow."""
    assert _compose_env()["YTT_PATH_PREFIX"] == _expected_default_literal(
        "YTT_PATH_PREFIX"
    )


_SELFHOSTING_PIN_DOCS = {
    "README.md": _README,
    "docs/usage/self-hosting.md": _SELF_HOSTING,
}


@pytest.mark.parametrize("doc_name", list(_SELFHOSTING_PIN_DOCS))
def test_selfhosting_docs_pin_the_release_image(doc_name):
    """Each self-hosting document pins exactly the release image — the
    pytest-side twin of the DoD script's markdown leg, so the Docker build
    gate enforces it too."""
    doc = _SELFHOSTING_PIN_DOCS[doc_name]
    pins = set(re.findall(r"ronaldraygun/ytt:(\d+\.\d+\.\d+)", doc))
    assert pins == {VERSION}, (
        f"{doc_name} pins {sorted(pins)}, VERSION is {VERSION} — bump every "
        "copy in the same release commit"
    )


# ---------------------------------------------------------------------------
# Leg 6 — canary surface: the Settings row plus the compile-time knobs
# ---------------------------------------------------------------------------

#: The canary's knobs outside the ``Settings`` schema — the fixed probe-video
#: ladder and the dedicated metrics port are constants in ``ytt/canary.py``,
#: not env vars, so the Settings-derived legs above never see them.  This leg
#: holds every surface that restates them to the source instead.
_CANARY_SOURCE = (REPO_ROOT / "ytt" / "canary.py").read_text(encoding="utf-8")


def _readme_canary_note() -> str:
    """The README's compile-time canary note (below the Configuration table).

    Its presence is part of the contract: without it the probe ladder and the
    metrics port are back to being documented only incidentally — in an
    evidence note and the manifests — which is the drift this leg exists for
    (bead ytt-7ff3501a).
    """
    m = re.search(r"Two canary knobs are compile-time.*?(?=\n\n)", _README, re.DOTALL)
    assert m, (
        "README lost its compile-time canary note (the CANARY_VIDEO_IDS "
        "ladder and the metrics port) — restore it under §Configuration"
    )
    return m.group(0)


def test_both_documents_carry_the_canary_interval_row():
    """``YTT_CANARY_INTERVAL_SEC`` — the canary's only Settings knob — keeps
    a row in both tables stating the model default.  The literal-default leg
    only checks rows that exist, so without this presence pin the README's
    curated table could silently drop the row again."""
    for doc_name, doc in _DOCS.items():
        cell = _default_cell(doc, "YTT_CANARY_INTERVAL_SEC")
        assert cell is not None, f"{doc_name} lost its YTT_CANARY_INTERVAL_SEC row"
        expected = f"`{_expected_default_literal('YTT_CANARY_INTERVAL_SEC')}`"
        assert expected in cell, (
            f"{doc_name} states default {cell!r} for YTT_CANARY_INTERVAL_SEC; "
            f"Settings says {expected}"
        )


def test_readme_canary_note_names_the_probe_ladder_in_order():
    """The backticked 11-character video ids in the README's canary note are
    exactly :data:`ytt.canary.CANARY_VIDEO_IDS`, in ladder order — a probe
    video added, removed or reordered in ``ytt/canary.py`` must update the
    README in the same commit."""
    ids = re.findall(r"`([A-Za-z0-9_-]{11})`", _readme_canary_note())
    assert ids == list(CANARY_VIDEO_IDS), (
        f"README canary note names {ids}; ytt/canary.py's ladder is "
        f"{list(CANARY_VIDEO_IDS)}"
    )


def test_canary_metrics_port_agrees_across_source_docs_and_manifests():
    """One metrics port everywhere: the ``start_http_server`` literal in
    ``ytt/canary.py``, the README's canary note, the configuration guide's
    canary section, and the canary Deployment's containerPort + Service
    port/targetPort."""
    m = re.search(r"start_http_server\((\d+)", _CANARY_SOURCE)
    assert m, "ytt/canary.py no longer states its metrics port as a literal"
    port = m.group(1)
    assert f":{port}" in _readme_canary_note(), (
        f"README canary note does not name the metrics port :{port}"
    )
    assert re.search(rf"on\s+:{port}\b", _GUIDE), (
        f"configuration.md's canary section no longer serves metrics on :{port}"
    )
    deployments = [
        doc
        for _, doc in _manifest_docs("Deployment")
        if doc.get("metadata", {}).get("name") == "ytt-canary"
    ]
    assert len(deployments) == 1, "expected exactly one ytt-canary Deployment"
    ports = [
        str(container_port["containerPort"])
        for container in deployments[0]["spec"]["template"]["spec"]["containers"]
        for container_port in container.get("ports", [])
    ]
    assert ports == [port], (
        f"ytt-canary containerPorts {ports} != metrics port {port}"
    )
    services = [
        doc
        for _, doc in _manifest_docs("Service")
        if doc.get("metadata", {}).get("name") == "ytt-canary"
    ]
    assert len(services) == 1, "expected exactly one ytt-canary Service"
    service_ports = [
        (str(p["port"]), str(p["targetPort"])) for p in services[0]["spec"]["ports"]
    ]
    assert service_ports == [(port, port)], (
        f"ytt-canary Service ports {service_ports} != metrics port {port}"
    )
