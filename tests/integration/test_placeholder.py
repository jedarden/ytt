"""Integration suite marker placeholder.

Integration tests hit real YouTube / Whisper and only pass inside
``ardenone-cluster`` (datacenter IPs are blocked elsewhere — this box included).
They are excluded from the default/gating run (``pytest -m "not integration"``).
The real scenarios (1-6, 8, 11, 12, 14 + load test) are added in Phase 9.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


def test_integration_marker_is_registered():
    # Sanity: this file is collected only under `-m integration`. It must never
    # run in the default unit gate. Real scenarios land in Phase 9.
    assert True
