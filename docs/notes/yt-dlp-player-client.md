# yt-dlp Player-Client Pin & the PoToken Avoidance Contract

## The contract

`ytt` extracts YouTube captions and (fallback) audio **cookie-free, with no
PoToken provider, from a datacenter IP**. The only thing that makes that
possible is *player-client choice*: every yt-dlp call in the codebase pins

```python
YDL_EXTRACTOR_ARGS = {
    "extractor_args": {"youtube": {"player_client": ["tv", "web_embedded", "mweb"]}},
}
```

(`ytt/fetch.py` — the single source of truth, merged into `YDL_BASE_OPTS` for
the caption path and re-spread in `ytt/whisper.py::_do_download_audio` for the
Whisper `bestaudio` download; plan §Fetch core step 1 requires the same
override on both paths.)

YouTube's PoToken (BotGuard) scheme gates three request contexts per client —
**GVS** (media streams), **Player** (Innertube player), and **Subs**
(`timedtext` captions). A client whose context is gated returns *skipped
formats* or *empty caption bodies* unless a PO token is supplied. ytt supplies
none — by policy (plan §Security: no cookies, no third-party token provider on
a datacenter IP) — so every client it pins must keep its needed contexts
ungated. The full research, including the 2025-era `web`-client subtitle rot
and the bgutil provider alternative we deliberately do not ship, lives in
`docs/research/yt-dlp-caption-extraction.md`.

## Why order matters

yt-dlp tries the listed clients in order and merges what they yield:

| Position | Client | Role | Policy in pinned yt-dlp (2026.07.04) |
|---|---|---|---|
| 1 | `tv` | **Primary extractor — the media donor.** Its formats feed the Whisper `bestaudio` download. | clean: no GVS/Subs PO gate, no sign-in |
| 2 | `web_embedded` | First fallback; fully clean | clean: no PO gate of any context |
| 3 | `mweb` | Caption-path diversity only. Its **media is GVS-PO-gated** (`https`+`dash`) — yt-dlp silently skips those formats — but its subtitle path is clean, so it never hurts captions and is never relied on for audio. | GVS-gated (`https`, `dash`); Subs clean |

Clients explicitly forbidden:

- **`web`** — GVS-PO-gated media plus the most aggressive bot checks; forcing
  it is the classic "captions silently come back empty" mistake (plan §Fetch
  core step 1 forbids it by name).
- **`tv_downgraded`, `web_creator`** — `REQUIRE_AUTH` in yt-dlp's own policy
  data: they demand an authenticated account for every video, which ytt
  cannot and will not supply.

## The failure modes are all silent

yt-dlp is pinned to an exact version (`pyproject.toml`) precisely because its
YouTube extractor rots fast — but the pin is *bumped regularly*, and every
bump re-rolls YouTube's client policies without any compile-time signal. The
runtime failures, should the pin drift onto a gated client, are:

1. A client name yt-dlp no longer knows → warn-only
   `Skipping unsupported client …`, quiet degradation toward yt-dlp's own
   default rotation.
2. A GVS-gated client's formats → skipped outright
   (`_report_pot_format_skipped`); the Whisper path loses `bestaudio`.
3. A Subs-gated client's caption tracks → discarded
   (`_report_pot_subtitles_skipped`); users see `empty_body`.
4. A renamed extractor key (`youtube`) or arg (`player_client`) → yt-dlp never
   sees the override at all; no warning, total silence.

All four surface in production only as missing captions or doomed Whisper
jobs — which is why the contract is enforced in CI, against yt-dlp's own
structured policy data, instead of being trusted.

## Enforcement — `tests/unit/test_ytdlp_contract.py`

The contract module reads the *installed* distribution's
`INNERTUBE_CLIENTS` (with its `GVS_PO_TOKEN_POLICY` / `SUBS_PO_TOKEN_POLICY` /
`REQUIRE_AUTH` entries) and drives a real `YoutubeDL` through the production
opts — all offline. On a breaking yt-dlp bump it fails in CI naming the broken
assumption and the fix, instead of in production as empty caption tracks:

| Test | Catches |
|---|---|
| `test_installed_ytdlp_matches_the_pyproject_pin` | the tested yt-dlp is not the declared one (env drift, skipped `uv sync`) |
| `test_every_pinned_client_exists_in_installed_ytdlp` | failure mode 1 — unknown/renamed client names |
| `test_no_pinned_client_requires_sign_in` | a pinned client flipped to `REQUIRE_AUTH` |
| `test_no_pinned_client_requires_a_subs_po_token` | failure mode 3 — caption tracks would be discarded |
| `test_primary_client_media_formats_are_not_gvs_po_token_gated` | failure mode 2 on the client that donates `bestaudio` (leading position only — `mweb`'s known GVS gate is why it rides last) |
| `test_po_token_gated_or_auth_clients_are_never_pinned` | tripwire against adding an auth/subs-gated client later |
| `test_the_web_client_is_never_pinned` | tripwire against "simplifying" onto `web` |
| `test_ytdlp_actually_receives_the_pinned_player_clients` | failure mode 4 — plumbing broke and yt-dlp silently ignores the override |
| `test_whisper_audio_download_pins_the_same_clients` | the audio-download path dropping the override, the no-cookies keys, or `bestaudio` |

Run it with the rest of the suite: `scripts/definition-of-done.sh` (or
`uv run pytest tests/unit/test_ytdlp_contract.py -q` for just this module).
The live-network half of the assumption — that the pinned clients actually
work from the deployed egress IP — is the canary's job (`ytt canary --once`,
plan §Proof Obligations; see `docs/research/residential-egress-options.md`).

## Rotation SOP — when the contract test fails

The test message names the broken assumption; the fix is always one of:

1. **Rotate** (`client renamed/removed`, `auth` or `subs` gate appeared):
   pick replacements from the installed yt-dlp's own data —
   `uv run python -c "from yt_dlp.extractor.youtube._base import
   INNERTUBE_CLIENTS; print(INNERTUBE_CLIENTS)"` — requiring: no
   `REQUIRE_AUTH`, no required `SUBS_PO_TOKEN_POLICY`, and (for whichever
   client leads the list) no required `GVS_PO_TOKEN_POLICY`. Cross-check the
   official PO Token Guide and re-derive the table above.
2. **Re-order** (a GVS gate appeared mid-list): move a media-clean client to
   the front; a caption-clean-but-media-gated client may stay for caption
   diversity but never first.
3. **Bump with intent**: update the table in this doc to the new installed
   version's observed policies in the same commit as the `pyproject.toml`
   pin bump — a table row that contradicts the installed yt-dlp is exactly
   the silent drift this contract exists to prevent.

If no clean client remains, that is a plan-level decision (ship a PO-token
provider or a residential egress change) — not something to fix inside this
pin. Escalate; do not quietly pin a gated client and accept empty captions.

Plan references: §Fetch core step 1, §Security, §Proof Obligations, §Risk
register ("yt-dlp options stay valid").
