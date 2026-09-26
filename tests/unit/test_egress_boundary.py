"""Egress-boundary regression guard: no third-party transcript API, no PoToken provider.

The documentation promise this module enforces:

- README intro: "All transcript fetching happens **inside the server** — no
  third-party transcript APIs." Transcripts come from exactly two places —
  yt-dlp talking to YouTube (cookie-free, PoToken-avoiding player clients,
  ``docs/notes/yt-dlp-player-client.md``) and the configured
  ``YTT_WHISPER_URL`` ASR service for caption-less videos.
- ``docs/notes/proxy-egress.md``: the full traffic table — what dials where,
  what may ride the residential proxy, what never does.
- ``docs/research/managed-transcript-apis.md``: the managed transcript APIs
  (Supadata, TranscriptAPI, ...) that were evaluated and *rejected*. The
  names below are tripwires, not options.

Two legs, both running in the CI gate (the Argo ``ytt-build`` pipeline runs
the unit suite through ``scripts/definition-of-done.sh``):

**Static surface checks** — the runtime dependency list, the installed
distribution set (including yt-dlp's plugin namespace), the package's own
imports, and the ``Settings`` URL surface are all closed. Transcript
retrieval cannot be re-pointed at a third party without adding a dependency,
a plugin, an import, or a config URL — and each of those surfaces fails HERE,
naming the boundary, instead of shipping.

**Mocked-network runtime tests** — both production paths (caption fetch via
:func:`ytt.fetch.fetch_transcript`; ASR via :func:`ytt.whisper.run_whisper_job`
plus the startup model guard) are driven end-to-end through their REAL code
with the network faked only at its true boundaries: a recording ``YoutubeDL``
stand-in and an ``httpx.MockTransport``. An autouse socket-level tripwire
fails the run — at teardown even if a broad ``except`` swallowed it — on ANY
attempt to open a real connection. A mocked boundary that silently stops
being mocked, or a new egress call added to either path, lands here instead
of in production traffic.

All tests are offline: no network, no real extraction, no DNS.
"""

from __future__ import annotations

import ast
import importlib.metadata
import importlib.util
import io
import json
import os
import re
import socket
import tomllib
from pathlib import Path
from urllib.parse import urlparse
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from ytt.config import Settings
from ytt.fetch import YDL_EXTRACTOR_ARGS, fetch_transcript
from ytt.whisper import WhisperJobRegistry, check_model_guard, run_whisper_job

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DIR = REPO_ROOT / "ytt"

VIDEO_ID = "dQw4w9WgXcQ"
WATCH_URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"
#: Where yt-dlp actually serves json3 caption bodies from — the realistic
#: shape the caption-path egress recorder asserts against.
TIMEDTEXT_URL = (
    "https://www.youtube.com/api/timedtext"
    "?v=dQw4w9WgXcQ&lang=en&fmt=json3&xorb=2&xobt=3&xovt=3"
)

# ---------------------------------------------------------------------------
# The boundary, as data
# ---------------------------------------------------------------------------

#: Runtime dependencies ytt may ship. Closed set: a new dependency must be
#: added here deliberately, which is the review moment for "can this package
#: reach a transcript API or PoToken provider?".
SANCTIONED_RUNTIME_DEPS: frozenset[str] = frozenset(
    {
        "fastmcp",
        "yt-dlp",
        "uvicorn",
        "httpx",
        "pydantic-settings",
        "prometheus-client",
        "structlog",
        "starlette",
    }
)

#: Substring patterns for distribution/import names that would breach the
#: boundary: managed transcript APIs rejected in
#: ``docs/research/managed-transcript-apis.md``, and PoToken providers
#: (bgutil-ytdlp-pot-provider and friends). Matched against normalized
#: (lowercase, ``_`` → ``-``) names.
FORBIDDEN_PACKAGE_PATTERNS: tuple[str, ...] = (
    "supadata",
    "transcriptapi",
    "transcript-api",
    "youtube-transcript",
    "tactiq",
    "notegpt",
    "youtubetotranscript",
    "pot-provider",
    "potoken",
    "po-token",
    "bgutil",
    "yt-dlp-get-pot",
)

#: Host patterns that must never appear in recorded egress on either
#: transcript path. ``ipinfo.io`` is the *sanctioned* egress-probe host
#: (``ytt.selftest``) — which is exactly why it is listed here: the probe is
#: a deliberate operator diagnostic outside these paths, and it must never
#: migrate into the fetch path.
FORBIDDEN_EGRESS_HOST_PATTERNS: tuple[str, ...] = (
    "supadata.ai",
    "transcriptapi.com",
    "tactiq",
    "notegpt",
    "pot-provider",
    "bgutil",
    "ipinfo.io",
)

#: Hosts yt-dlp legitimately serves YouTube data from (suffix match, so
#: ``i.ytimg.com`` matches ``ytimg.com``).
YOUTUBE_HOST_SUFFIXES: frozenset[str] = frozenset(
    {
        "youtube.com",
        "youtube-nocookie.com",
        "googlevideo.com",
        "ytimg.com",
    }
)
#: Exact hosts that are YouTube-controlled but share a broader domain we
#: deliberately do NOT allowlist wholesale (``googleapis.com`` is far too
#: wide to suffix-match).
YOUTUBE_EXACT_HOSTS: frozenset[str] = frozenset({"youtubei.googleapis.com"})


def _host_is_youtube(host: str) -> bool:
    return host in YOUTUBE_EXACT_HOSTS or any(
        host == suffix or host.endswith("." + suffix)
        for suffix in YOUTUBE_HOST_SUFFIXES
    )


def _forbidden_host_pattern(host: str) -> str | None:
    for pattern in FORBIDDEN_EGRESS_HOST_PATTERNS:
        if pattern in host:
            return pattern
    return None


def _assert_recorded_hosts_are_sanctioned(
    what: str,
    urls: list[str],
    *,
    whisper_url: str | None = None,
) -> None:
    """Every recorded URL must be YouTube- or (for ASR) the configured Whisper."""
    whisper_hosts = {urlparse(whisper_url).hostname} if whisper_url else set()
    for url in urls:
        host = urlparse(url).hostname or ""
        forbidden = _forbidden_host_pattern(host)
        assert not forbidden, (
            f"{what} recorded egress to {host!r} (matched forbidden pattern "
            f"{forbidden!r}) — a third-party transcript API / PoToken "
            "provider / out-of-scope host on the transcript path. This "
            "breaks the documented no-third-party promise (README intro, "
            "docs/notes/proxy-egress.md)."
        )
        assert _host_is_youtube(host) or host in whisper_hosts, (
            f"{what} recorded egress to {host!r}, which is neither a "
            "YouTube-controlled host nor the configured Whisper service "
            f"({sorted(h for h in whisper_hosts if h)}). The transcript "
            "paths may only talk to YouTube (yt-dlp) and YTT_WHISPER_URL — "
            "see docs/notes/proxy-egress.md."
        )


# ---------------------------------------------------------------------------
# Socket-level tripwire: no test here may open a real connection
# ---------------------------------------------------------------------------


class EgressViolation(AssertionError):
    """A real connection was attempted inside the mocked-network guard."""


class _EgressTripwire:
    """Records and refuses every connection attempt at the socket layer.

    Raising (instead of silently failing the call) turns an unmocked egress
    attempt into an immediate, host-naming failure; recording as well means a
    broad ``except Exception`` in production code cannot swallow the
    violation — the fixture teardown re-asserts the record is empty.
    """

    def __init__(self) -> None:
        self.attempts: list[str] = []

    def refuse(self, syscall: str, address: object) -> None:
        if isinstance(address, (tuple, list)) and address:
            host = address[0]
        else:
            host = address
        desc = f"{syscall}({host!r})"
        self.attempts.append(desc)
        raise EgressViolation(
            f"egress-boundary tripwire fired: {desc}. The caption/ASR tests "
            "in this module must run fully mocked — a real connection here "
            "means a mocked boundary stopped being mocked, or new egress "
            "code was added to a transcript path. Host must be YouTube "
            "(via yt-dlp) or YTT_WHISPER_URL, and must be faked in-test "
            "(docs/notes/proxy-egress.md)."
        )


@pytest.fixture(autouse=True)
def _no_real_egress(monkeypatch: pytest.MonkeyPatch):
    """Arm the tripwire for every test in this module; re-check at teardown.

    The syscall replacements are plain functions (not bound methods) so they
    bind correctly when assigned onto ``socket.socket``. The teardown
    re-assert exists because production code legitimately catches broad
    exceptions around network calls (e.g. the Whisper model guard) — a
    violation raised inside such a try-block would otherwise be logged and
    swallowed, and the test would pass on a path that reached for the real
    network.
    """
    tripwire = _EgressTripwire()

    def refused_connect(sock, address, *args: object, **kwargs: object):
        tripwire.refuse("socket.connect", address)

    def refused_connect_ex(sock, address, *args: object, **kwargs: object):
        tripwire.refuse("socket.connect_ex", address)

    def refused_create_connection(address, *args: object, **kwargs: object):
        tripwire.refuse("socket.create_connection", address)

    def refused_getaddrinfo(host, port, *args: object, **kwargs: object):
        tripwire.refuse("socket.getaddrinfo", host)

    monkeypatch.setattr(socket.socket, "connect", refused_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", refused_connect_ex)
    monkeypatch.setattr(socket, "create_connection", refused_create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", refused_getaddrinfo)
    yield tripwire
    assert not tripwire.attempts, (
        "swallowed egress attempt(s) recorded during this test: "
        f"{tripwire.attempts} — production code caught the tripwire's "
        "exception, but the attempt itself still breaks the boundary "
        "(see the fixture docstring)."
    )


class TestTripwireIsArmed:
    """The guard must be able to fail: prove each patched syscall trips it.

    A tripwire that silently stopped intercepting (stdlib refactor, runner
    change) would leave every other test in this module looking enforced
    while nothing was. Each test clears the recorder afterwards so the
    fixture teardown sees a clean record.
    """

    def test_socket_connect_is_refused(self, _no_real_egress: _EgressTripwire) -> None:
        sock = socket.socket()
        try:
            with pytest.raises(EgressViolation, match="api.supadata.ai"):
                sock.connect(("api.supadata.ai", 443))
        finally:
            sock.close()
        _no_real_egress.attempts.clear()

    def test_create_connection_is_refused(
        self, _no_real_egress: _EgressTripwire
    ) -> None:
        with pytest.raises(EgressViolation, match="pot-provider.example"):
            socket.create_connection(("pot-provider.example", 8080))
        _no_real_egress.attempts.clear()

    def test_getaddrinfo_is_refused(self, _no_real_egress: _EgressTripwire) -> None:
        with pytest.raises(EgressViolation, match="api.transcriptapi.com"):
            socket.getaddrinfo("api.transcriptapi.com", 443)
        _no_real_egress.attempts.clear()


# ---------------------------------------------------------------------------
# Static leg 1: the dependency surface
# ---------------------------------------------------------------------------


def _runtime_dependency_names() -> set[str]:
    """Normalized distribution names from ``[project].dependencies``."""
    pyproject = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    names: set[str] = set()
    for requirement in pyproject["project"]["dependencies"]:
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", requirement.strip())
        assert match, f"unparseable requirement in pyproject.toml: {requirement!r}"
        names.add(match.group(1).lower().replace("_", "-"))
    return names


class TestDependencySurface:
    """CI check: the runtime dependency list stays inside the boundary."""

    def test_runtime_dependencies_are_allowlisted(self) -> None:
        """Any new runtime dependency (a transcript API SDK, a PoToken
        provider, anything) must land in SANCTIONED_RUNTIME_DEPS first —
        which forces the boundary review at the moment of the change."""
        unknown = _runtime_dependency_names() - SANCTIONED_RUNTIME_DEPS
        assert not unknown, (
            f"pyproject.toml adds runtime dependency(ies) {sorted(unknown)} "
            "outside the egress-boundary allowlist. ytt fetches transcripts "
            "only via yt-dlp (YouTube) plus the configured YTT_WHISPER_URL; "
            "a new dependency that can reach a managed transcript API or a "
            "PoToken provider must not ship (README intro; "
            "docs/research/managed-transcript-apis.md). If the dependency "
            "is genuinely transcript-egress-free, add it to "
            "SANCTIONED_RUNTIME_DEPS in tests/unit/test_egress_boundary.py "
            "with a comment saying why."
        )

    def test_no_dependency_name_matches_a_forbidden_pattern(self) -> None:
        """Tripwire in the other direction: even if someone extends the
        allowlist carelessly, a denylisted distribution name still fails."""
        hits = [
            name
            for name in _runtime_dependency_names()
            if any(pattern in name for pattern in FORBIDDEN_PACKAGE_PATTERNS)
        ]
        assert not hits, (
            f"pyproject.toml declares {hits} — a managed transcript API or "
            "PoToken provider dependency. These were evaluated and rejected "
            "(docs/research/managed-transcript-apis.md); shipping one "
            "contradicts the documented cookie-free, no-third-party "
            "posture."
        )


class TestInstalledDistributionSurface:
    """CI check: nothing PoToken-provider-shaped is installed alongside yt-dlp."""

    def test_no_pot_provider_plugin_or_transcript_api_distribution(self) -> None:
        """PoToken providers ship as yt-dlp plugins (distribution or the
        ``yt_dlp_plugins`` namespace / yt_dlp entry points); transcript API
        SDKs ship as ordinary distributions. Either, present in the resolved
        environment, is a breach regardless of what the source imports."""
        violations: list[str] = []

        for dist in importlib.metadata.distributions():
            name = (dist.metadata.get("Name") or "").lower().replace("_", "-")
            if any(pattern in name for pattern in FORBIDDEN_PACKAGE_PATTERNS):
                violations.append(f"distribution {name!r} {dist.version} is installed")

        plugin_entry_points = [
            ep for ep in importlib.metadata.entry_points() if "yt_dlp" in ep.group
        ]
        if plugin_entry_points:
            violations.append(
                "yt-dlp plugin entry point(s) registered: "
                f"{[f'{ep.group}:{ep.name}' for ep in plugin_entry_points]}"
            )

        try:
            spec = importlib.util.find_spec("yt_dlp_plugins")
        except (ModuleNotFoundError, ValueError):
            spec = None
        if spec is not None:
            violations.append(
                "the yt_dlp_plugins namespace package exists "
                f"({spec.origin!r}) — a yt-dlp plugin (the shape PoToken "
                "providers take) is importable"
            )

        assert not violations, (
            "the environment breaches the no-PoToken-provider / "
            "no-transcript-API boundary: " + "; ".join(violations) + ". "
            "Remove it (pyproject.toml + uv.lock, then `uv sync`) — ytt "
            "avoids PoTokens through player-client choice, not a provider."
        )


# ---------------------------------------------------------------------------
# Static leg 2: the source import surface
# ---------------------------------------------------------------------------

#: Per-file allowlist for network-capable imports in ``ytt/``. The caption
#: path (``fetch.py``), the ASR path (``whisper.py``) and the canary probe
#: (``canary.py`` — the same caption fetch, watched) may use yt-dlp; the
#: egress probe (``selftest.py``) is the one sanctioned non-YouTube httpx
#: caller (ipinfo.io — outside the transcript paths, see
#: FORBIDDEN_EGRESS_HOST_PATTERNS). ``derived_url.py`` dials nothing of its
#: own — it wraps yt-dlp's request entry points to enforce the derived-URL
#: allowlist (docs/notes/derived-url-policy.md) on the egress the rows
#: above dial.
NETWORK_IMPORTS_BY_FILE: dict[str, set[str]] = {
    "fetch.py": {"yt_dlp"},
    "whisper.py": {"yt_dlp", "httpx"},
    "canary.py": {"yt_dlp"},
    "selftest.py": {"httpx"},
    "derived_url.py": {"yt_dlp"},
}

#: Stdlib modules that can open connections and must never be imported by
#: the package (matched against the full dotted name).
FORBIDDEN_DOTTED_IMPORTS: frozenset[str] = frozenset(
    {
        "urllib.request",
        "http.client",
        "ftplib",
        "smtplib",
        "telnetlib",
        "xmlrpc.client",
    }
)

#: Third-party transport libraries — banned as import roots anywhere in the
#: package: all egress rides yt_dlp or httpx, so none of these has a reason
#: to exist here. (``httpx``'s own core, ``httpcore``, is likewise not a
#: sanctioned import surface.)
FORBIDDEN_NETWORK_ROOTS: frozenset[str] = frozenset(
    {
        "requests",
        "urllib3",
        "aiohttp",
        "websockets",
        "grpc",
        "pycurl",
        "httpcore",
    }
)

#: ``socket`` is stdlib and can open connections, but ``singleton.py`` uses
#: it only for ``gethostname`` (local, no connection). Anywhere else is a
#: violation; anywhere at all, the runtime tripwire is the backstop.
SANCTIONED_SOCKET_IMPORTS: dict[str, set[str]] = {
    "singleton.py": {"socket"},
}


def _imported_dotted_names(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.append(node.module)
    return names


class TestSourceImportSurface:
    """CI check: the package's network-capable imports stay enumerated."""

    def test_no_forbidden_or_unenumerated_network_import(self) -> None:
        violations: list[str] = []
        for source in sorted(PACKAGE_DIR.glob("*.py")):
            seen: set[str] = set()
            for dotted in _imported_dotted_names(source):
                if dotted in seen:  # one violation per module, not per import site
                    continue
                seen.add(dotted)
                root = dotted.split(".")[0]
                normalized = dotted.lower().replace("_", "-")
                if dotted in FORBIDDEN_DOTTED_IMPORTS:
                    violations.append(
                        f"{source.name} imports {dotted!r} — a stdlib module "
                        "that opens connections; route it through the "
                        "sanctioned surfaces (yt_dlp / httpx)"
                    )
                elif root in FORBIDDEN_NETWORK_ROOTS:
                    violations.append(
                        f"{source.name} imports {dotted!r} — a transport "
                        "library outside the sanctioned surfaces (yt_dlp / "
                        "httpx); all egress must ride those two"
                    )
                elif root == "socket" and source.name not in SANCTIONED_SOCKET_IMPORTS:
                    violations.append(
                        f"{source.name} imports socket — only singleton.py "
                        "may (gethostname, local-only); connections belong "
                        "to yt_dlp / httpx"
                    )
                elif any(
                    pattern in normalized for pattern in FORBIDDEN_PACKAGE_PATTERNS
                ):
                    violations.append(
                        f"{source.name} imports {dotted!r} — a third-party "
                        "transcript API / PoToken provider module"
                    )
                elif root in ("yt_dlp", "httpx"):
                    allowed = NETWORK_IMPORTS_BY_FILE.get(source.name, set())
                    if root not in allowed:
                        violations.append(
                            f"{source.name} imports {root} — not in "
                            f"NETWORK_IMPORTS_BY_FILE for this file {sorted(allowed)}; "
                            "a new network caller in the package must be "
                            "added to the egress-boundary guard deliberately"
                        )
        assert not violations, (
            "ytt/ package imports breach the egress boundary:\n  - "
            + "\n  - ".join(violations)
        )


# ---------------------------------------------------------------------------
# Static leg 3: the configuration URL surface
# ---------------------------------------------------------------------------


class TestConfigUrlSurface:
    def test_settings_url_fields_are_exactly_the_sanctioned_set(self) -> None:
        """The only URL-shaped settings may be the ones the boundary names:
        the ASR endpoint, the egress proxy, and the inbound/OAuth pair. A new
        URL field (``YTT_TRANSCRIPT_API_URL``, say) cannot appear without
        updating this guard — the deliberate-change moment."""
        url_fields = {name for name in Settings.model_fields if "url" in name}
        assert url_fields == {
            "whisper_url",
            "proxy_url",
            "public_url",
            "oidc_config_url",
        }, (
            f"Settings grew URL field(s) {sorted(url_fields - {'whisper_url', 'proxy_url', 'public_url', 'oidc_config_url'})}. "
            "Transcript egress is yt-dlp→YouTube plus YTT_WHISPER_URL; "
            "proxy_url only re-routes that same traffic; public_url / "
            "oidc_config_url are inbound/OAuth. A new URL setting that can "
            "point transcript retrieval at a third party must not ship — "
            "update this guard only after the boundary review."
        )


# ---------------------------------------------------------------------------
# Runtime leg: mocked-network drives of the real caption and ASR paths
# ---------------------------------------------------------------------------


def _make_settings(**overrides: object) -> Settings:
    """Real ``Settings`` with test-safe values and a sentinel Whisper URL.

    The sentinel host (``whisper.test.local``) is deliberately neither
    YouTube nor any real service, so a hardcoded third-party URL cannot
    masquerade as the configured one.
    """
    env = {
        "YTT_ALLOWED_SUBJECTS": "test-sub",
        "YTT_CACHE_BACKEND": "emptydir",
        "YTT_CACHE_DIR": "/tmp",
        "YTT_SCRATCH_DIR": "/tmp",
        "YTT_EXTRACT_TIMEOUT_SEC": "60",
        "YTT_WHISPER_URL": "http://whisper.test.local:8000",
        **{f"YTT_{k.upper()}": str(v) for k, v in overrides.items()},
    }
    with patch.dict(os.environ, env, clear=False):
        return Settings()


def _assert_no_cookie_opts(what: str, opts: dict) -> None:
    """The cookie-free half of the promise, on the *constructed* runtime opts."""
    assert "cookiefile" in opts and "cookiesfrombrowser" in opts, (
        f"{what} dropped the no-cookies keys entirely (they must be present "
        "and None to block config-file cookies)"
    )
    assert not opts["cookiefile"] and not opts["cookiesfrombrowser"], (
        f"{what} no longer enforces cookie-free extraction"
    )


def _assert_player_clients_pinned(what: str, opts: dict) -> None:
    assert opts.get("extractor_args") == YDL_EXTRACTOR_ARGS["extractor_args"], (
        f"{what} does not carry the PoToken-avoiding player-client pin "
        f"({YDL_EXTRACTOR_ARGS['extractor_args']!r})"
    )


class _RecordingYdl:
    """YoutubeDL stand-in that records every network-bound call it receives.

    Records ``extract_info`` (the metadata/caption-track extraction, which
    dials youtube.com) and ``urlopen`` (the json3 body fetch) — the two
    outbound calls the real caption path makes.
    """

    def __init__(
        self,
        opts: dict,
        info: dict,
        json3_bytes: bytes,
        calls: list[tuple[str, str]],
    ) -> None:
        self._opts = opts
        self._info = info
        self._json3 = json3_bytes
        self._calls = calls

    def __enter__(self) -> "_RecordingYdl":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def extract_info(self, url: str, download: bool = False) -> dict:
        self._calls.append(("extract_info", url))
        return self._info

    def urlopen(self, url: str) -> io.BytesIO:
        self._calls.append(("urlopen", url))
        return io.BytesIO(self._json3)


class _RecordingAudioYdl:
    """YoutubeDL stand-in for the audio download (extract_info + download)."""

    def __init__(self, opts: dict, info: dict, calls: list[tuple[str, str]]) -> None:
        self._opts = opts
        self._info = info
        self._calls = calls

    def __enter__(self) -> "_RecordingAudioYdl":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def extract_info(self, url: str, download: bool = False) -> dict:
        self._calls.append(("extract_info", url))
        return self._info

    def download(self, urls: list[str]) -> None:
        for url in urls:
            self._calls.append(("download", url))


def _json3_body() -> bytes:
    return json.dumps(
        {
            "events": [
                {
                    "tStartMs": 0,
                    "dDurationMs": 2000,
                    "segs": [{"utf8": "hello "}, {"utf8": "world"}],
                }
            ]
        }
    ).encode("utf-8")


class TestCaptionPathEgress:
    """``fetch_transcript`` must egress to YouTube only, cookie-free."""

    async def test_caption_fetch_records_youtube_only_egress(self) -> None:
        settings = _make_settings()
        calls: list[tuple[str, str]] = []
        opts_captured: list[dict] = []
        info = {
            "id": VIDEO_ID,
            "title": "Test Video",
            "channel": "Test Channel",
            "duration": 120.0,
            "upload_date": "20240101",
            "language": "en",
            "subtitles": {"en": [{"ext": "json3", "url": TIMEDTEXT_URL}]},
            "automatic_captions": {},
        }

        def ydl_factory(opts: dict) -> _RecordingYdl:
            opts_captured.append(opts)
            return _RecordingYdl(opts, info, _json3_body(), calls)

        with patch("ytt.fetch.yt_dlp.YoutubeDL", side_effect=ydl_factory):
            result = await fetch_transcript(VIDEO_ID, None, settings)

        # The real production flow ran to completion (not a stub): the json3
        # body was parsed into segments through the sanctioned pair.
        assert result.source == "caption_manual", (
            "test harness broke: the caption flow did not run to completion"
        )
        assert result.segments, "caption flow produced no segments"

        # Egress record: exactly the two YouTube dials the path should make.
        assert calls == [
            ("extract_info", WATCH_URL),
            ("urlopen", TIMEDTEXT_URL),
        ], (
            f"caption path made unexpected calls: {calls!r} — every dial "
            "must be the yt-dlp watch-URL extraction plus the json3 "
            "caption-body fetch"
        )
        _assert_recorded_hosts_are_sanctioned("caption path", [url for _, url in calls])

        # Cookie-free + PoToken-avoiding, on the opts yt-dlp actually got.
        assert len(opts_captured) == 1, "caption path built YoutubeDL more than once"
        _assert_no_cookie_opts("caption path", opts_captured[0])
        _assert_player_clients_pinned("caption path", opts_captured[0])
        # Direct egress: no proxy configured, none injected.
        assert "proxy" not in opts_captured[0], (
            "caption path set a proxy without YTT_PROXY_URL configured"
        )


class TestAsrPathEgress:
    """The ASR path must egress to YouTube (audio) + the configured Whisper only."""

    async def test_whisper_job_downloads_from_youtube_and_posts_to_configured_whisper(
        self, tmp_path: Path
    ) -> None:
        settings = _make_settings(scratch_dir=str(tmp_path / "scratch"))
        scratch = Path(settings.scratch_dir)
        scratch.mkdir(parents=True)
        (scratch / f"{VIDEO_ID}.m4a").write_bytes(b"fake audio bytes")

        ytdlp_calls: list[tuple[str, str]] = []
        opts_captured: list[dict] = []
        audio_info = {"duration": 50.0, "filesize": 1024}

        def audio_ydl_factory(opts: dict) -> _RecordingAudioYdl:
            opts_captured.append(opts)
            return _RecordingAudioYdl(opts, audio_info, ytdlp_calls)

        http_log: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            http_log.append((request.method, str(request.url)))
            return httpx.Response(
                200,
                json={
                    "text": "hello world",
                    "language": "en",
                    "segments": [{"start": 0.0, "end": 1.0, "text": "hello world"}],
                },
            )

        registry = WhisperJobRegistry()
        cache = MagicMock()
        cache.put = AsyncMock(return_value=True)
        job, _ = await registry.get_or_create(VIDEO_ID, 50.0, settings)

        with patch("ytt.whisper.yt_dlp.YoutubeDL", side_effect=audio_ydl_factory):
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)
            ) as http_client:
                await run_whisper_job(
                    job,
                    registry,
                    settings,
                    cache,
                    settings.whisper_model,
                    http_client=http_client,
                )

        final = await registry.get(VIDEO_ID)
        assert final is not None and final.status == "done", (
            f"ASR flow did not complete (status {getattr(final, 'status', None)!r}) "
            "— the egress record below proves nothing"
        )
        cache.put.assert_called_once()

        # The only HTTP call is the transcription POST to the CONFIGURED
        # Whisper service — no third-party ASR, no token provider.
        assert http_log == [
            ("POST", f"{settings.whisper_url}/v1/audio/transcriptions")
        ], (
            f"ASR path made unexpected HTTP calls: {http_log!r} — expected "
            "exactly one POST to YTT_WHISPER_URL /v1/audio/transcriptions"
        )
        _assert_recorded_hosts_are_sanctioned(
            "ASR path (httpx)",
            [url for _, url in http_log],
            whisper_url=settings.whisper_url,
        )

        # The audio leg dials YouTube only, through the pinned cookie-free opts.
        assert ytdlp_calls == [
            ("extract_info", WATCH_URL),
            ("download", WATCH_URL),
        ], f"audio download made unexpected yt-dlp calls: {ytdlp_calls!r}"
        _assert_recorded_hosts_are_sanctioned(
            "ASR path (yt-dlp)", [url for _, url in ytdlp_calls]
        )
        assert len(opts_captured) == 1, "audio path built YoutubeDL more than once"
        _assert_no_cookie_opts("audio download", opts_captured[0])
        _assert_player_clients_pinned("audio download", opts_captured[0])
        assert opts_captured[0].get("format") == "bestaudio", (
            "audio download no longer requests bestaudio"
        )

    async def test_model_guard_queries_only_the_configured_whisper(self) -> None:
        """The startup ``GET /v1/models`` probe is Whisper-bound too."""
        settings = _make_settings()
        http_log: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            http_log.append((request.method, str(request.url)))
            return httpx.Response(200, json={"data": [{"id": settings.whisper_model}]})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http_client:
            model = await check_model_guard(
                settings.whisper_url,
                settings.whisper_model,
                http_client=http_client,
            )

        assert model == settings.whisper_model
        assert http_log == [("GET", f"{settings.whisper_url}/v1/models")], (
            f"model guard made unexpected HTTP calls: {http_log!r}"
        )
        _assert_recorded_hosts_are_sanctioned(
            "model guard",
            [url for _, url in http_log],
            whisper_url=settings.whisper_url,
        )
