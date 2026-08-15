"""Authorization — subject allowlist (plan: Security & Authorization).

Checks the token's ``email`` claim against ``YTT_ALLOWED_SUBJECTS`` on every
tool call; empty allowlist = deny all (fail-closed); not listed → ``403``
with a human-readable message ("Contact the server operator to be added to
the allowlist").

NOTE (2026-08-15, post-ADR-003 Google -> Authentik migration): this used to
also require the ``email_verified`` claim, which was meaningful under
Google (a real third-party verification signal). It is NOT meaningful
against this deployment's Authentik instance -- confirmed live via the
"authentik default OAuth Mapping: OpenID 'email'" scope mapping, whose
expression hardcodes ``"email_verified": False`` unconditionally for every
account, with no path to ever return True short of editing that
instance-wide managed mapping (shared by every other OAuth2 provider on
sso.ardenone.com, e.g. ibkr-mcp, OpenBao -- too broad a blast radius to
change for ytt's sake alone). Dropped the check instead: this Authentik
instance already gates access to the ytt application itself via a
platform-admins group policy binding, and accounts are entirely
operator-created with no external federation, so "verified" was never
protecting against anything real in this model.

Two entry points:

- ``check_subject_auth`` — the real enforcement point. Wired into
  ``ytt/server.py`` as global FastMCP ``AuthMiddleware``, so it runs before
  every tool call, resource read, and prompt render over the actual MCP
  transport (not just a diagnostic route). This closes the gap in a prior
  version of this module, where ``check_subject`` was only ever called from
  the ``/admin/egress`` side route — the transcript tools themselves had no
  allowlist check at all.
- ``check_subject`` — the lower-level allow/deny primitive, still used
  directly by ``/admin/egress`` (which authenticates itself out-of-band from
  the MCP transport) and by unit tests.

The allowlist is enforced **at tool invocation**, not at token issuance (plan:
"Token issuance succeeds for any user who completes the OAuth flow — the token
proves identity; authorization is a separate decision.").

Sub-discovery mechanism (plan: "selftest --show-sub"):
  On the first successful auth, write the raw ``sub`` to
  ``/tmp/ytt_last_sub`` (mode 0600) so operators can run
  ``ytt selftest --show-sub`` to find the exact string to add to
  ``YTT_ALLOWED_SUBJECTS``. The redaction filter blocks ``sub`` from logs;
  this temp-file mechanism is intentionally NOT a log. (Largely a formality
  now that ``sub`` = a human-readable Google account email the operator
  already knows, but kept for parity with the original discovery flow.)
"""

from __future__ import annotations

import os

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


def subject_allowed(subject: str, allowed: frozenset[str]) -> bool:
    """Return True if *subject* is permitted by the *allowed* allowlist.

    Matching is **case-insensitive** and supports two entry kinds:

    - a full subject/email (exact match), e.g. ``me@jedcabanero.com``;
    - a **domain pattern** beginning with ``@``, e.g. ``@jedcabanero.com``,
      which matches any subject whose email is in that exact domain. The
      leading ``@`` anchors the match to the whole domain, so
      ``@jedcabanero.com`` matches ``anyone@jedcabanero.com`` but NOT
      ``x@evil-jedcabanero.com`` (different domain) nor
      ``x@sub.jedcabanero.com`` (subdomain).

    Domain patterns are safe here because the ``email`` claim comes from
    this deployment's Authentik instance, whose accounts are entirely
    operator-created with no self-signup or external identity federation —
    there's no path for a caller to present an arbitrary email address
    Authentik didn't itself assign to a real, admin-managed account. (This
    no longer relies on the IdP's ``email_verified`` claim — see
    ``check_subject_auth`` and the module docstring for why that stopped
    being a meaningful signal after the Authentik migration.)

    Args:
        subject: The subject/email to test.
        allowed: The parsed allowlist (``Settings.allowed_subjects_set``); may
            mix exact and ``@domain`` entries.
    """
    s = subject.strip().lower()
    allowed_norm = {a.strip().lower() for a in allowed}
    if s in allowed_norm:
        return True
    return any(entry.startswith("@") and s.endswith(entry) for entry in allowed_norm)


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
    if not subject_allowed(sub, allowed):
        raise YttError(
            FORBIDDEN,
            "Access denied. Contact the server operator to be added to the allowlist.",
        )
    # First successful auth: write sub to temp file for selftest --show-sub.
    write_last_sub(sub)


# ---------------------------------------------------------------------------
# AuthMiddleware check — the real per-tool-call enforcement point
# ---------------------------------------------------------------------------


def check_subject_auth(ctx) -> bool:
    """``fastmcp.server.middleware.AuthMiddleware`` auth check.

    Runs before every tool call, resource read, and prompt render (see
    ``ytt/server.py`` — ``AuthMiddleware(auth=check_subject_auth)``).

    ``ctx.token`` is the FastMCP ``AccessToken`` FastMCP resolved for this
    request, already signature/audience-verified by the Authentik-federated
    provider (``ytt.auth.build_auth_provider``). Its ``claims`` dict carries
    the ID token's decoded payload, including ``email`` (but not a
    meaningful ``email_verified`` — see module docstring).

    Returns ``False`` (deny) rather than raising, matching
    ``fastmcp.utilities.authorization.AuthCheck``'s callable contract — a
    ``False`` return is what ``AuthMiddleware`` turns into an
    ``AuthorizationError`` for the caller.
    """
    if ctx.token is None:
        return False

    claims = ctx.token.claims or {}
    email = claims.get("email")

    if not email:
        return False

    from ytt.config import get_settings

    settings = get_settings()
    allowed = subject_allowed(email, settings.allowed_subjects_set)
    if allowed:
        write_last_sub(email)
    return allowed
