"""Authorization — subject allowlist (plan: Security & Authorization).

Checks the token ``sub`` claim against ``YTT_ALLOWED_SUBJECTS`` on every tool
call; empty allowlist = deny all (fail-closed); not listed → ``403`` with a
human-readable message ("Contact the server operator to be added to the
allowlist").

The allowlist is enforced **at tool invocation**, not at token issuance (plan:
"Token issuance succeeds for any user who completes the OAuth flow — the token
proves identity; authorization is a separate decision.").

Sub-discovery mechanism (plan: "selftest --show-sub"):
  On the first successful auth, write the raw ``sub`` to
  ``/tmp/ytt_last_sub`` (mode 0600) so operators can run
  ``ytt selftest --show-sub`` to find the exact string to add to
  ``YTT_ALLOWED_SUBJECTS``. The redaction filter blocks ``sub`` from logs;
  this temp-file mechanism is intentionally NOT a log.
"""

from __future__ import annotations

import os
import stat

from ytt.errors import FORBIDDEN, YttError

# ---------------------------------------------------------------------------
# Temp-file path for selftest --show-sub mechanism
# ---------------------------------------------------------------------------
_LAST_SUB_PATH = "/tmp/ytt_last_sub"


def write_last_sub(sub: str) -> None:
    """Write *sub* to the last-sub temp file (mode 0600, once per process).

    The ``_written_subs`` set ensures we only call open() once per ``sub``
    value; in practice a single server process sees one sub per connector so
    this is effectively "write once". Thread-safe because the Python GIL
    serialises the set lookup+add; asyncio ensures coroutines don't interleave
    between the lookup and the write.
    """
    if sub in _written_subs:
        return
    _written_subs.add(sub)
    try:
        fd = os.open(_LAST_SUB_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(sub)
    except OSError:
        # Non-fatal: selftest --show-sub won't work, but auth continues.
        pass


_written_subs: set[str] = set()


def get_last_sub() -> str | None:
    """Read the last-sub temp file; return ``None`` if it doesn't exist."""
    try:
        with open(_LAST_SUB_PATH, "r") as f:
            return f.read().strip() or None
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Subject allowlist check
# ---------------------------------------------------------------------------


def check_subject(sub: str, allowed: frozenset[str]) -> None:
    """Raise :class:`~ytt.errors.YttError` (FORBIDDEN) if *sub* is not allowed.

    Plan: "Empty allowlist = deny all (fail-closed)."
    Plan: "Contact the server operator to be added to the allowlist."

    The ``sub`` value is deliberately NOT included in the error message because
    the message is verbatim-relayed by the model (end-user visible).

    Args:
        sub: The token ``sub`` claim value.
        allowed: The parsed allowlist from ``Settings.allowed_subjects_set``.

    Raises:
        YttError: With ``error_code=FORBIDDEN`` if not in the allowlist.
    """
    if sub not in allowed:
        raise YttError(
            FORBIDDEN,
            "Access denied. Contact the server operator to be added to the allowlist.",
        )
    # First successful auth: write sub to temp file for selftest --show-sub.
    write_last_sub(sub)
