"""In-cluster proof that the configured ``YTT_PROXY_URL`` is actually used.

Contract: ``docs/notes/proxy-egress.md``. These tests run ONLY inside
ardenone-cluster (``pytest -m integration`` — datacenter IPs elsewhere are
blocked by YouTube, which is exactly the condition they exercise). They skip
automatically via ``tests/integration/conftest.py`` when the server is
unreachable or ``YTT_TEST_TOKEN`` is unset.

What "actually used" means here — asserted, not assumed:

- ``GET /admin/egress`` reports ``via_proxy: true`` (the server's egress probe
  dialed ipinfo through the proxy) and ``is_residential: true`` (the proxy's
  exit IP is a residential one — the whole point of the proxy);
- ``ytt canary --once --via-proxy`` fetches captions THROUGH the proxy
  (``caption_fetch.via_proxy is true``, ``verdict == "ok"``) — the end-to-end
  check that YouTube traffic flows over the proxy.
"""

from __future__ import annotations

import pytest

from tests.integration.conftest import ADMIN_EGRESS_URL, http_client  # noqa: F401

pytestmark = pytest.mark.integration


def _admin_headers() -> dict:
    import os

    return {"Authorization": f"Bearer {os.environ['YTT_TEST_TOKEN']}"}


def test_admin_egress_reports_proxied_residential_egress(http_client):
    """The server's egress probe must dial THROUGH YTT_PROXY_URL and classify
    the proxy's exit IP as residential."""
    resp = http_client.get(ADMIN_EGRESS_URL, headers=_admin_headers())
    assert resp.status_code == 200, f"admin/egress failed: {resp.status_code}"
    data = resp.json()
    assert data.get("via_proxy") is True, (
        f"server egress probe did not dial through the proxy: {data} — is "
        "YTT_PROXY_URL set on the Deployment?"
    )
    assert data.get("is_residential") is True, (
        f"proxy egress IP is not residential: {data}"
    )


def test_canary_via_proxy_fetches_captions_through_the_proxy():
    """``run_once(via_proxy=True)``: a real caption fetch succeeds through the
    proxy — the configured proxy actually carries YouTube traffic."""
    from ytt.canary import run_once

    report = run_once(via_proxy=True)
    fetch = report.get("caption_fetch", {})
    assert fetch.get("via_proxy") is True, f"caption probe dialed direct: {report}"
    assert report.get("verdict") == "ok", (
        f"caption fetch through the proxy failed: {report}"
    )
    egress = report.get("egress", {})
    assert egress.get("is_residential") is True, (
        f"proxy egress not residential: {egress}"
    )
