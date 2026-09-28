"""No-Whisper startup puts nothing on the Whisper wire — the boot slice's
no-contact half, *measured* (bead ``ytt-e4ce76ef``, slice 1 of the
``ytt-ab79a7e8`` caption-only chain).

``test_no_whisper_boot.py`` (bead ``ytt-a3e32eb0``) already pins the boot
itself under both no-Whisper env shapes: Settings constructs, the real
``build_asgi_app()`` app enters its lifespan, and the deployment's liveness
probe answers green. What it deliberately leaves inferred is the wire: its
tolerance leg is structural (the reference default is in-cluster DNS a test
process cannot reach, so a boot that *blocked* on Whisper fails loudly) and
its hermeticity leg is the harness's fail-hard stubs — neither one
*measures* what the boot put on the wire, and neither would notice a boot
that quietly dials Whisper and swallows the error.

That seam exists and is one wiring commit away from being live.
``check_model_guard`` is documented as a startup probe — its docstring
carries the plan quote "on startup query GET /v1/models …" — and
``ytt/server.py``'s ``active_model`` comment still says "updated by
check_model_guard() at startup", yet no production call site wires it into
the lifespan today. The moment someone does, the **unset** shape starts
dialing the reference ``…/v1/models`` at every boot (and the empty shape
hands httpx a scheme-less URL), so "no ASR network call during startup"
stops being true of a deployment the ASR-RUNBOOK §5 still calls no-Whisper.
This module pins the measured claim instead: under both shapes, boot — app
build, lifespan, first liveness probe — hands **zero** requests to the
Whisper wire.

The measurement is the counted-transport pattern of
``test_no_whisper_captionless.py`` (``_CountingASRTransport``, imported, not
re-derived): a transport that records every URL it is handed and refuses it,
so whatever was attempted is on the log and whatever the transport was never
handed never left the process.

Scope of the count — the tree has exactly two Whisper wire URLs, both in
``ytt/whisper.py``: the guard's ``GET {whisper_url}/v1/models`` (in
``check_model_guard``) and the job's ``POST {whisper_url}/v1/audio/
transcriptions`` (in ``run_whisper_job``). Each gets the mechanism its shape
allows:

- the **guard** rides the counted transport for real — it is the realistic
  boot drift (a lifespan wiring), so the count is wire-level where it
  matters;
- the **job** stays at the harness's fail-hard stub (``hermetic_egress``):
  a boot that started an ASR job would need a job object no lifespan has,
  and the stub fails the call before any download or POST could run.
  Routing it through the transport instead would execute a real
  ``run_whisper_job`` body on a hypothetical — a wider blast radius than a
  boot pin needs, measuring a scenario that cannot occur.

Neither zero is allowed to be vacuous, so each test proves its counter
live: after the boot it pushes one real ``check_model_guard`` probe through
the wrapped seam and watches the transport record and refuse it (the guard's
documented fail-soft — "use configured name" — makes the probe safe). The
boot's zero therefore reads "the wire was clean while the counter was
watching", not "nothing was ever wired in" — and the strict list equality
would catch a boot-time dial too, since it would sit in the log ahead of
the probe.

The known ASGI trap is handled where the whole boot-slice family handles it:
``httpx.ASGITransport`` does not run ASGI lifespan, and ``open_asgi_client()``
(from ``tests/unit/_mcp_asgi_harness.py``) enters the app's own
``lifespan_context`` around the client — the hook uvicorn drives — so the
lifespan really ran, and the health request proves the routes live under it.
The env manipulation is the ``test_no_whisper_boot.py`` recipe verbatim:
``monkeypatch`` the variable, ``get_settings.cache_clear()``, hold the
resolved runtime settings, boot, and let the harness's
``allowlisted_subject`` fixture clear the cache again on the way out.
"""

from __future__ import annotations

import httpx

from ytt import whisper as ytt_whisper
from ytt.config import Settings, get_settings
from tests.unit._mcp_asgi_harness import open_asgi_client
from tests.unit._mcp_asgi_harness import (  # noqa: F401
    allowlisted_subject,
    authorized_bearer,
    hermetic_egress,
)
from tests.unit.test_no_whisper_captionless import _CountingASRTransport

#: The declared default — the reference in-cluster endpoint an env-free
#: resolution must arrive at (the same literal ``test_no_whisper_boot.py``
#: and ``test_asr_runbook.py`` pin against the schema).
DEFAULT_WHISPER_URL = "http://whisper-openai.whisper-stt.svc.cluster.local:8000"

#: The liveness probe path the deployment's httpGet targets — unauthenticated
#: by contract (docs/notes/http-endpoints.md §"/ytt/health — liveness").
HEALTH_PATH = "/ytt/health"


async def _probe_liveness(client: httpx.AsyncClient) -> None:
    """The deployment's own liveness probe, exactly as the kubelet sends it:
    a tokenless GET answering ``{"status": "ok"}`` and nothing more."""
    resp = await client.get(HEALTH_PATH)
    assert resp.status_code == 200, (
        f"liveness probe failed: HTTP {resp.status_code} {resp.text[:200]}"
    )
    assert resp.json() == {"status": "ok"}


#: The pristine model guard, bound at import time (before any patching) so
#: the wrapper below can never capture a previous test's replacement.
_REAL_GUARD = ytt_whisper.check_model_guard


def _count_the_guard_seam(
    monkeypatch, transport: _CountingASRTransport
) -> None:
    """Put the real ``check_model_guard`` on *transport*.

    The guard runs unmodified, over a client whose only wire is *transport*:
    any request it tries to make is recorded, then refused with
    ``ConnectError`` — indistinguishable, to the guard, from the unreachable
    Whisper its fail-soft path exists for (runbook §5: startup is never
    blocked)."""
    async def guard_over_the_counted_transport(whisper_url, whisper_model, **kwargs):
        kwargs.pop("http_client", None)
        async with httpx.AsyncClient(transport=transport) as client:
            return await _REAL_GUARD(whisper_url, whisper_model, http_client=client)

    monkeypatch.setattr(
        ytt_whisper, "check_model_guard", guard_over_the_counted_transport
    )


async def test_boot_with_whisper_url_unset_hands_whisper_nothing(
    monkeypatch,
) -> None:
    """Shape (a) — no ``YTT_WHISPER_URL`` at all: the resolved endpoint is
    the reference default (a real endpoint, never a disabled spelling), the
    real app builds and serves its liveness probe, and the counted transport
    ends the boot with an empty log: startup dialed nothing."""
    monkeypatch.delenv("YTT_WHISPER_URL", raising=False)
    get_settings.cache_clear()

    # The config loads, and the resolution is the declared reference default
    # (the same runtime leg test_no_whisper_boot.py holds).
    resolved = get_settings()
    assert resolved.whisper_url == DEFAULT_WHISPER_URL

    transport = _CountingASRTransport([])
    _count_the_guard_seam(monkeypatch, transport)

    async with open_asgi_client() as client:  # build + lifespan = the boot
        await _probe_liveness(client)

    # The boot put nothing on the Whisper wire — no /v1/models probe, no
    # transcription POST.
    assert transport.requested == []

    # Non-vacuity: the same seam, driven now, is recorded and refused — the
    # empty log above is a measurement, not a disconnected counter.
    active = await ytt_whisper.check_model_guard(
        resolved.whisper_url, resolved.whisper_model
    )
    assert active == resolved.whisper_model  # fail-soft: refused, not fatal
    assert transport.requested == [f"{DEFAULT_WHISPER_URL}/v1/models"]


async def test_boot_with_whisper_url_empty_hands_whisper_nothing(
    monkeypatch,
) -> None:
    """Shape (b) — ``YTT_WHISPER_URL=""`` (the documented caption-only
    deployment): empty survives construction verbatim as its own resolution
    (never coerced to the reference default), the app still boots green, and
    the counted transport again ends the boot with an empty log."""
    monkeypatch.setenv("YTT_WHISPER_URL", "")
    get_settings.cache_clear()

    resolved = get_settings()
    assert resolved.whisper_url == ""
    # Empty is a *distinct* resolution, not a spelling of unset — the
    # deliberate no-Whisper deployment must stay distinguishable.
    assert resolved.whisper_url != Settings.model_fields["whisper_url"].default

    transport = _CountingASRTransport([])
    _count_the_guard_seam(monkeypatch, transport)

    async with open_asgi_client() as client:
        await _probe_liveness(client)

    assert transport.requested == []

    # Non-vacuity, and the empty-URL artifact: the probe the transport is
    # handed is the scheme-less "/v1/models" — the same no-endpoint artifact
    # test_no_whisper_captionless.py pins for the job's POST, so even a boot
    # that regressed into dialing could not reach any endpoint this way.
    active = await ytt_whisper.check_model_guard(
        resolved.whisper_url, resolved.whisper_model
    )
    assert active == resolved.whisper_model
    assert transport.requested == ["/v1/models"]
