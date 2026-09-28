"""No-Whisper boot regression — the ASGI app starts without a configured
Whisper endpoint, under both no-Whisper env shapes (bead ``ytt-a3e32eb0``,
the boot slice of the caption-only regression coverage).

``deploy/ASR-RUNBOOK.md`` §5 ("Whisper unset (no-Whisper mode)") makes two
kinds of promise this module pins end to end — not at the Settings schema
(``test_asr_runbook.py`` does that) nor at the single model-guard seam
(``test_boot_tolerates_an_absent_whisper_service`` there), but through the
whole shipping boot path ``serve()`` uses: ``build_asgi_app()`` → lifespan →
a live route answering.

Resolution — *"``YTT_WHISPER_URL`` has a default (the reference in-cluster
endpoint …) — unset never means disabled, it means 'point at the reference
service'. Deliberate no-Whisper operation is a deployment choice: set the
variable to an unreachable address … or to an empty value."* The tests hold
the resolved runtime settings — the ``get_settings()`` instance the app and
its tools actually read — against both halves:

- **unset** must construct Settings env-free (a Settings that fails to
  build is a boot that never happened — CrashLoop, not no-Whisper mode) and
  resolve to exactly the declared reference default: a real endpoint, never
  a disabled spelling. The schema-level half of that chain is already
  pinned by ``test_unset_whisper_url_declares_reference_default_not_disabled``
  in ``test_asr_runbook.py``; this module closes it at the env→runtime
  boundary the schema pin cannot see.
- **empty** must survive construction verbatim as its own distinct
  resolution — not rejected at boot and not silently coerced back to the
  reference default. Either collapse would make the runbook's documented
  deliberate no-Whisper deployment ("an empty value") indistinguishable
  from plain unset, and its per-job consequences (``pending`` then
  ``asr_failed``, nothing cached — the §5 acceptance pinned in
  ``test_asr_runbook.py``) unobservable.

Tolerance — *"startup is **not** blocked by an absent/unreachable Whisper
(the boot-time model guard swallows connection errors by design), health
stays green."* The reference default is an in-cluster DNS name no test
process can reach, so a boot that required Whisper reachability would fail
here loudly — in the lifespan — rather than pass. The probe is the
deployment's own liveness probe: unauthenticated ``GET /ytt/health``
(``deployment.yml`` ``httpGet: /ytt/health :8080``; docs/notes/
http-endpoints.md §"/ytt/health — liveness"), the exact request the kubelet
sends and one that carries no token by contract.

Shared infrastructure: the tests ride ``tests/unit/_mcp_asgi_harness.py``
(``open_asgi_client`` — the real ``build_asgi_app()`` app under
``httpx.ASGITransport`` with the app's own lifespan entered, the hook
uvicorn drives) rather than re-deriving app/auth setup, and import the
harness's three autouse fixtures so the hermetic-egress guard applies: if
a boot ever grew a network seam, its stubs fail the test instead of the
network answering. The env manipulation follows the ``test_mcp_path_
prefix_mounting.py`` recipe: ``monkeypatch`` the variable, then
``get_settings.cache_clear()`` so the next read reconstructs from it (the
harness's ``allowlisted_subject`` fixture clears the cache again on the way
out — after monkeypatch has restored the env — so later modules never see
these resolutions).
"""

from __future__ import annotations

import httpx

from ytt.config import Settings, get_settings
from tests.unit._mcp_asgi_harness import open_asgi_client
from tests.unit._mcp_asgi_harness import (  # noqa: F401
    allowlisted_subject,
    authorized_bearer,
    hermetic_egress,
)

#: The declared default — the reference in-cluster endpoint the runbook's
#: §5 "unset never means disabled" sentence names; the same literal
#: ``test_asr_runbook.py`` pins as ``DEFAULT_WHISPER_URL`` against the
#: Settings schema. Here it is the value an env-free *runtime* resolution
#: must arrive at.
DEFAULT_WHISPER_URL = "http://whisper-openai.whisper-stt.svc.cluster.local:8000"

#: The liveness probe path the deployment's httpGet targets — unauthenticated
#: by contract (docs/notes/http-endpoints.md §"/ytt/health — liveness").
HEALTH_PATH = "/ytt/health"


async def _probe_liveness(client: httpx.AsyncClient) -> None:
    """The deployment's own liveness probe, exactly as the kubelet sends it:
    a tokenless GET answering ``{"status": "ok"}`` and nothing more —
    liveness only, no sensitive detail."""
    resp = await client.get(HEALTH_PATH)
    assert resp.status_code == 200, (
        f"liveness probe failed: HTTP {resp.status_code} {resp.text[:200]}"
    )
    assert resp.json() == {"status": "ok"}


async def test_boot_with_whisper_url_unset_resolves_the_reference_default(
    monkeypatch,
) -> None:
    """No ``YTT_WHISPER_URL`` at all: Settings constructs env-free, the
    running server resolves the built-in reference default (runbook §5 —
    "unset never means disabled, it means 'point at the reference
    service'"), and the app starts with the liveness probe green."""
    monkeypatch.delenv("YTT_WHISPER_URL", raising=False)
    get_settings.cache_clear()

    # The full unset→default chain, each half against the same literal the
    # runbook names: the schema declares it (the half
    # test_asr_runbook.py's unset pin holds) and an env-free construction
    # arrives at it (the half only a real get_settings() rebuild shows).
    assert Settings.model_fields["whisper_url"].default == DEFAULT_WHISPER_URL
    resolved = get_settings()
    assert resolved.whisper_url == DEFAULT_WHISPER_URL

    async with open_asgi_client() as client:  # lifespan up = startup ran
        await _probe_liveness(client)

    # The boot neither rewrote nor re-resolved the endpoint: the instance
    # the tools consult now is still the reference-default one it built
    # under (get_settings() is the same lru_cache slot build_asgi_app()
    # just read).
    assert get_settings().whisper_url == DEFAULT_WHISPER_URL


async def test_boot_with_whisper_url_empty_is_the_no_whisper_deployment(
    monkeypatch,
) -> None:
    """``YTT_WHISPER_URL=""`` — the runbook's other no-Whisper shape
    ("set the variable … to an empty value"): empty survives construction
    verbatim as its own resolution (never coerced to the reference default),
    and the app still starts with health green — the runbook's "startup is
    not blocked by an absent/unreachable Whisper … health stays green"."""
    monkeypatch.setenv("YTT_WHISPER_URL", "")
    get_settings.cache_clear()

    resolved = get_settings()
    assert resolved.whisper_url == ""
    # Empty is a *distinct* resolution, not a spelling of unset: the
    # deployment that configured it deliberately must stay distinguishable
    # from one that merely left the variable out.
    assert resolved.whisper_url != Settings.model_fields["whisper_url"].default

    async with open_asgi_client() as client:
        await _probe_liveness(client)

    assert get_settings().whisper_url == ""
