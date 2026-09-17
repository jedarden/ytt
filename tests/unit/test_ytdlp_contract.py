"""Contract tests: yt-dlp pin ↔ YouTube player-client / PoToken posture.

ytt avoids YouTube's PoToken (BotGuard) requirements purely through
player-client choice: ``YDL_EXTRACTOR_ARGS`` pins ``player_client`` to
``[tv, web_embedded, mweb]`` (plan §Fetch core step 1; rationale and the
per-client policy table live in ``docs/notes/yt-dlp-player-client.md``).
yt-dlp is pinned to an exact version in pyproject.toml — but that pin is
*bumped regularly* (the extractor rots fast), and every bump silently
re-rolls YouTube's client policies.

The runtime failure modes are all SILENT, which is why this module exists:

- a client name yt-dlp no longer knows is skipped with a warning and the
  configured list quietly degrades toward yt-dlp's own default rotation;
- a GVS-PO-token-gated client's media formats are *skipped* (Whisper path
  loses ``bestaudio``);
- a subs-PO-token-gated client's caption tracks are *discarded*;
- a renamed extractor key/arg means yt-dlp never sees the override at all.

Each test below reads yt-dlp's *own* structured policy data
(``INNERTUBE_CLIENTS`` and its ``*PoTokenPolicy`` entries) from the
installed distribution — or drives a real ``YoutubeDL`` through the
production opts — so a breaking yt-dlp update fails HERE, naming the
broken assumption and the fix, instead of in production as empty caption
tracks (the 2025.5.22-style rot).

All tests are offline: no network, no real extraction.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yt_dlp

from ytt.fetch import YDL_BASE_OPTS, YDL_EXTRACTOR_ARGS, YDL_NO_COOKIES

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The player clients ytt ships — the single source of truth is the
#: production constant itself, so a deliberate edit to it is what these
#: tests validate against the installed yt-dlp (never the reverse).
PINNED_CLIENTS: list[str] = YDL_EXTRACTOR_ARGS["extractor_args"]["youtube"][
    "player_client"
]

#: yt-dlp docs/research both single out the plain ``web`` client as the one
#: whose subtitle/media path is PO-gated and whose bot-check is the most
#: aggressive — plan §Fetch core step 1 forbids it explicitly.
FORBIDDEN_CLIENTS: frozenset[str] = frozenset({"web"})


# ---------------------------------------------------------------------------
# Reading yt-dlp's own client/PoToken policy data (installed distribution)
# ---------------------------------------------------------------------------


def _load_client_policies() -> dict[str, dict]:
    """Return yt-dlp's ``INNERTUBE_CLIENTS`` from the *installed* distribution.

    Clients whose name starts with ``_`` are excluded — yt-dlp itself forbids
    explicitly requesting those (they cannot appear in ``player_client``).
    """
    try:
        from yt_dlp.extractor.youtube._base import INNERTUBE_CLIENTS
    except ImportError as exc:
        pytest.fail(
            "yt-dlp no longer exposes INNERTUBE_CLIENTS at "
            "yt_dlp.extractor.youtube._base — the structured client/PoToken "
            "data this contract is verified against has moved or been "
            "removed. Re-derive the PoToken posture of the pinned "
            "player clients from the new location (PO Token Guide: "
            "https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide), update "
            "docs/notes/yt-dlp-player-client.md, and only then trust the "
            f"player_client pin. (ImportError: {exc})"
        )
    return {
        client: cfg
        for client, cfg in INNERTUBE_CLIENTS.items()
        if not client.startswith("_")
    }


def _gvs_po_token_required_protocols(client_cfg: dict) -> set[str]:
    """Protocols whose media formats require a GVS PO Token for this client.

    Media formats from a client with a required protocol are *skipped* by
    yt-dlp when no token is supplied (``_report_pot_format_skipped``). A
    missing ``GVS_PO_TOKEN_POLICY`` entry means "not required" — the default
    yt-dlp applies to the currently-clean clients (``tv``, ``web_embedded``).
    """
    policy = client_cfg.get("GVS_PO_TOKEN_POLICY") or {}
    return {
        getattr(proto, "value", str(proto))
        for proto, cfg in policy.items()
        if getattr(cfg, "required", False)
    }


def _subs_po_token_required(client_cfg: dict) -> bool:
    """Does this client's subtitle path require a PO Token (caption path)?

    Gated caption tracks are *discarded* by yt-dlp when no token is
    supplied (``_report_pot_subtitles_skipped``). A missing
    ``SUBS_PO_TOKEN_POLICY`` means "not required".
    """
    return bool(getattr(client_cfg.get("SUBS_PO_TOKEN_POLICY"), "required", False))


def _requires_sign_in(client_cfg: dict) -> bool:
    """``REQUIRE_AUTH`` clients need an authenticated account for every video.

    ytt runs cookie-free by policy (plan §Security), so an auth-required
    client can never serve it.
    """
    return bool(client_cfg.get("REQUIRE_AUTH", False))


# ---------------------------------------------------------------------------
# Version pin integrity
# ---------------------------------------------------------------------------


class TestVersionPinIntegrity:
    def test_installed_ytdlp_matches_the_pyproject_pin(self) -> None:
        """The yt-dlp being tested IS the yt-dlp ytt declares.

        The exact pin exists so extraction behavior is reproducible and so
        the client-contract tests below are meaningful; an environment that
        quietly runs a different yt-dlp invalidates both.
        """
        pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r'"yt-dlp==([^"]+)"', pyproject)
        assert match, (
            "pyproject.toml no longer pins yt-dlp to an exact == version — "
            "the reproducible-build contract is gone and these contract "
            "tests have nothing to assert against. Restore the pin."
        )
        declared, installed = match.group(1), yt_dlp.version.__version__

        def norm(version: str):
            # yt-dlp writes 2026.07.04 where pyproject may say 2026.7.4 —
            # compare numerically; non-numeric parts fall back to literals.
            try:
                return tuple(int(part) for part in version.split("."))
            except ValueError:
                return version

        assert norm(declared) == norm(installed), (
            f"Installed yt-dlp {installed!r} does not match the pyproject "
            f"pin {declared!r} — the environment drifted from the declared "
            "version (uv sync was skipped, or the venv was bumped by hand). "
            "Re-sync so the tested yt-dlp is the pinned yt-dlp; the "
            "player-client contract below is only meaningful against the "
            "declared version."
        )


# ---------------------------------------------------------------------------
# Player-client ↔ PoToken contract (the pin's reason to exist)
# ---------------------------------------------------------------------------


class TestPlayerClientContract:
    def test_every_pinned_client_exists_in_installed_ytdlp(self) -> None:
        """A client name yt-dlp doesn't know is dropped with a warn-only
        'Skipping unsupported client' — and if every name went unknown, the
        list degrades entirely to yt-dlp's own default rotation. Both are
        silent at runtime; this fails here instead, on a pin bump."""
        known = set(_load_client_policies())
        unknown = set(PINNED_CLIENTS) - known
        assert not unknown, (
            f"yt-dlp {yt_dlp.version.__version__} no longer knows pinned "
            f"player client(s) {sorted(unknown)} — it would silently skip "
            f"them at runtime. yt-dlp's known clients: {sorted(known)}. "
            "Rotate YDL_EXTRACTOR_ARGS in ytt/fetch.py onto still-supported "
            "PoToken-clean clients (see docs/notes/yt-dlp-player-client.md) "
            "and update that doc's policy table."
        )

    def test_no_pinned_client_requires_sign_in(self) -> None:
        """ytt runs cookie-free (plan §Security); an auth-required client
        (REQUIRE_AUTH, e.g. web_creator/tv_downgraded) can never serve it."""
        policies = _load_client_policies()
        auth_gated = [
            client
            for client in PINNED_CLIENTS
            if _requires_sign_in(policies[client])
        ]
        assert not auth_gated, (
            f"Pinned player client(s) {auth_gated} now REQUIRE sign-in per "
            "yt-dlp's own policy data — extraction would demand cookies ytt "
            "forbids. Rotate the client list (docs/notes/"
            "yt-dlp-player-client.md)."
        )

    def test_no_pinned_client_requires_a_subs_po_token(self) -> None:
        """Caption path invariant: yt-dlp DISCARDS caption tracks that need
        an unsupplied PO token — this is the exact 'fakes no captions' rot.
        No pinned client may gate its subtitle path on a PO token."""
        policies = _load_client_policies()
        subs_gated = [
            client
            for client in PINNED_CLIENTS
            if _subs_po_token_required(policies[client])
        ]
        assert not subs_gated, (
            f"Pinned player client(s) {subs_gated} now require a PO Token "
            "for subtitles per yt-dlp's own policy data — caption tracks "
            "would be silently discarded (empty_body). Rotate the client "
            "list (docs/notes/yt-dlp-player-client.md)."
        )

    def test_primary_client_media_formats_are_not_gvs_po_token_gated(self) -> None:
        """Media path invariant (Whisper ``bestaudio`` fallback): the FIRST
        pinned client is the one whose extraction normally serves the
        download, so its formats must not require a GVS PO Token — gated
        formats are skipped outright when no token is supplied.

        Only the lead position is asserted: a caption-clean-but-media-gated
        client (``mweb`` today) may ride later in the list for caption
        diversity, since the leading client already donates the media. The
        ordering policy lives in docs/notes/yt-dlp-player-client.md."""
        primary = PINNED_CLIENTS[0]
        gated = _gvs_po_token_required_protocols(_load_client_policies()[primary])
        assert not gated, (
            f"Primary player client {primary!r} now requires a GVS PO Token "
            f"for {sorted(gated)} media per yt-dlp's own policy data — its "
            "formats would be skipped and the Whisper audio download would "
            "lose bestaudio. Re-order/rotate the client list so a "
            "media-clean client leads (docs/notes/yt-dlp-player-client.md)."
        )

    def test_po_token_gated_or_auth_clients_are_never_pinned(self) -> None:
        """Tripwire in the other direction: any client yt-dlp's own data
        marks PO-gated (captions) or sign-in-required is off-limits, so a
        future 'why not just add X?' edit gets caught before it ships."""
        policies = _load_client_policies()
        off_limits = {
            client
            for client, cfg in policies.items()
            if _subs_po_token_required(cfg) or _requires_sign_in(cfg)
        }
        pinned_off_limits = off_limits & set(PINNED_CLIENTS)
        assert not pinned_off_limits, (
            f"Pinned player client(s) {sorted(pinned_off_limits)} are "
            "PO-token-gated or sign-in-required per yt-dlp's own policy "
            "data — remove them from YDL_EXTRACTOR_ARGS "
            "(docs/notes/yt-dlp-player-client.md)."
        )

    def test_the_web_client_is_never_pinned(self) -> None:
        """plan §Fetch core step 1: never use ``web`` — its media path is
        GVS-PO-gated and its bot check is the most aggressive. Guarded as
        ytt's own decision so a future simplification can't reintroduce it."""
        pinned_web = set(PINNED_CLIENTS) & FORBIDDEN_CLIENTS
        assert not pinned_web, (
            f"{sorted(pinned_web)} client must never appear in "
            "YDL_EXTRACTOR_ARGS (plan §Fetch core step 1: PO-token-gated "
            "media + most aggressive bot check)."
        )

    def test_ytdlp_actually_receives_the_pinned_player_clients(self) -> None:
        """End-to-end plumbing, offline: a real YoutubeDL built from the
        production opts must deliver the client list to the YouTube
        extractor's configuration *unchanged*. Catches a renamed extractor
        key ('youtube'), a renamed arg ('player_client'), or a reshaped
        value — every shape of breakage yt-dlp answers with a warning-free
        'unknown extractor arg' and total silence."""
        from yt_dlp.extractor.youtube import YoutubeIE

        ydl = yt_dlp.YoutubeDL(dict(YDL_BASE_OPTS))
        seen = YoutubeIE(ydl)._configuration_arg("player_client", default=[])
        assert seen == PINNED_CLIENTS, (
            f"yt-dlp received player_client={seen!r} from YDL_BASE_OPTS — "
            f"expected {PINNED_CLIENTS!r}. The extractor-arg plumbing "
            "(key 'youtube', arg 'player_client') changed shape in this "
            "yt-dlp, meaning the PoToken-avoiding clients are NOT being "
            "applied. Fix YDL_EXTRACTOR_ARGS in ytt/fetch.py."
        )


# ---------------------------------------------------------------------------
# Every YouTube-bound surface must carry the contract
# ---------------------------------------------------------------------------


class TestEverySurfaceCarriesTheClients:
    def test_whisper_audio_download_pins_the_same_clients(self, tmp_path) -> None:
        """The Whisper fallback downloads media through the SAME
        PoToken-avoiding clients (plan §Fetch core step 1: 'the same
        extractor_args player_client override is required for the audio
        download path'). Regression-guard the merge in
        ``ytt.whisper._do_download_audio``."""
        from ytt.whisper import _do_download_audio

        captured: dict = {}

        def capturing_ydl(opts: dict) -> MagicMock:
            captured.update(opts)
            ydl = MagicMock()
            ydl.__enter__.return_value = ydl
            ydl.extract_info.return_value = {
                "formats": [
                    {
                        "format_id": "140",
                        "ext": "m4a",
                        "vcodec": "none",
                        "acodec": "mp4a.40.2",
                        "tbr": 128,
                        "filesize": 1024,
                    }
                ]
            }
            ydl.download.return_value = None
            return ydl

        # Pretend the download produced output so the fake flow completes.
        (tmp_path / "dQw4w9WgXcQ.m4a").write_bytes(b"x")

        with patch("yt_dlp.YoutubeDL", side_effect=capturing_ydl):
            out = _do_download_audio(
                "dQw4w9WgXcQ",
                str(tmp_path),
                max_audio_bytes=10_000,
            )

        assert out.endswith(".m4a"), (
            "test harness broke: the stubbed audio download did not run to "
            "completion, so the captured opts below prove nothing"
        )
        assert captured.get("extractor_args") == YDL_EXTRACTOR_ARGS[
            "extractor_args"
        ], "audio download path dropped the player-client override"
        # Presence AND falsy — a bare .get() is None for an absent key too,
        # which would let a silently-dropped enforcement pass.
        assert "cookiefile" in captured and "cookiesfrombrowser" in captured, (
            "audio download path dropped the no-cookies keys entirely "
            "(they must be present and None to block config-file cookies)"
        )
        assert not captured["cookiefile"] and not captured["cookiesfrombrowser"], (
            "audio download path no longer enforces no-cookies"
        )
        assert captured.get("format") == "bestaudio", (
            "audio download path no longer requests bestaudio"
        )
