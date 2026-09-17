# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Bounded download & Whisper resource guardrails** (bead `ytt-89d1e56d`). The
  ASR fallback path is now a bounded resource end to end — an allowlisted
  caller (or a fleet of them) cannot exhaust scratch disk, the shared Whisper
  service, or the network:
  - **Queued-work cap** — new `YTT_MAX_PENDING_WHISPER_JOBS` (default `16`)
    bounds the system's ASR *backlog* (pending + running jobs). Where the
    per-subject quota caps each subject's *rate*, this caps the backlog: a
    caption-less request that would start a *new* job while the queue is at
    capacity is denied with the stable `rate_limited` error ("Whisper queue
    full (…/…)"), spending no quota slot. Joining an in-flight job is always
    allowed — it adds no work. `0` = deny every new job (fail-closed, same
    convention as the per-subject limits; negative values are startup errors).
  - **Duration cap enforced at job creation** — the no-captions error now
    carries the video's duration from `extract_info` metadata
    (`ytt.errors.NoCaptionsError.duration_sec`), so a video over
    `YTT_MAX_ASR_DURATION_SEC` is refused `too_long_for_asr` *before* a job is
    registered, a quota slot is charged, or any audio is downloaded. A new
    download-time backstop re-checks the duration inside
    `ytt.whisper._do_download_audio` for videos whose duration was unknown at
    creation (or whose metadata changed since) — still before any bytes hit
    the wire.
  - **Scratch cleanup on every exit path** — each job attempt now sweeps its
    own `{video_id}.*` partial files after success *or* failure
    (`_sweep_video_scratch`). Previously a download that timed out or aborted
    mid-stream leaked its partial file (up to `YTT_MAX_AUDIO_BYTES` per
    failure) until the next restart's startup sweep; repeated failures of the
    same video could fill the scratch volume. Sweeping is safe against the
    zombie downloader: a timed-out `asyncio.to_thread` yt-dlp keeps writing
    from its thread, and deleting the file unlinks the name — the inode frees
    when the thread exits and the file can never outlive the process.
  - **Cancellation** — cancelling a job task releases its
    `YTT_MAX_CONCURRENT_WHISPER` slot (`async with`) and still runs the
    scratch sweep; a job that never reaches a terminal state is recovered by
    the stale-running GC (`timeout + TTL`), which frees the queue slot it
    pinned.
  Coverage: `tests/unit/test_whisper.py` (duration backstop, scratch sweep,
  `active_count` queue-depth signal, failed/cancelled-job hygiene, slot
  release) and `tests/unit/test_server.py` (queue-full denial without quota
  spend, join-while-full, check ordering, duration refusal at creation).

- **`YTT_PROXY_URL` end-to-end** (bead `ytt-8c702583`). The proxy contract is
  now specified, enforced, and tested — `docs/notes/proxy-egress.md` is the
  single spec. `Settings` validates the URL at startup (http/https only — no
  SOCKS, the httpx egress probe has no adapter; empty is a fail-closed error,
  not a silent unset; whitespace rejected). Caption extraction and the Whisper
  audio download share `ytt.fetch.run_with_proxy_retry`: direct first, exactly
  one proxied retry on `ip_blocked`, with defined failure behavior (retry
  timeout → `timeout_code` "(proxy retry also timed out)"; retry failure → the
  retry's `error_code` "(proxy retry also failed)"). The ASR POST and OAuth
  traffic are never proxied. Proxy credentials never reach logs or relayed
  errors: new `ytt.observability.redact_credentials()` strips `user:pass@`
  from free-text exception strings at every yt-dlp/httpx boundary (fetch,
  whisper, canary, `/admin/egress` 502). The egress probe now passes the
  httpx >= 0.28 singular `proxy=` kwarg (the removed `proxies=` silently
  degraded every proxy-configured egress report to "probe failed"). The canary
  gains `--via-proxy` (`ytt canary --once --via-proxy`) — the in-cluster
  end-to-end check that the proxy actually carries YouTube traffic. Mocked
  coverage in `tests/unit/test_proxy.py`; in-cluster proof in
  `tests/integration/test_proxy_live.py`.

- **yt-dlp player-client / PoToken contract tests + notes doc** (bead
  `ytt-70690b3d`). ytt avoids YouTube's PoToken (BotGuard) requirements
  purely through player-client choice (`YDL_EXTRACTOR_ARGS` pins
  `player_client` to `[tv, web_embedded, mweb]`), and every way that
  assumption breaks on an yt-dlp bump is *silent at runtime*: unknown
  client names are skipped with a warn-only message, PO-gated media
  formats are skipped, PO-gated caption tracks are discarded, and a
  renamed extractor key means the override is never applied at all. New
  `tests/unit/test_ytdlp_contract.py` verifies the pin against the
  *installed* yt-dlp's own structured policy data (`INNERTUBE_CLIENTS`:
  `GVS_PO_TOKEN_POLICY` / `SUBS_PO_TOKEN_POLICY` / `REQUIRE_AUTH`) and
  drives a real `YoutubeDL` through the production opts — offline — so a
  breaking bump fails in CI naming the broken assumption and the fix.
  The relied-on settings, the per-client policy table for the pinned
  version, why `mweb` (media-GVS-gated, caption-clean) rides last, and
  the rotation SOP now live in `docs/notes/yt-dlp-player-client.md`.

- **One-shot residential-egress canary** (`ytt canary --once`, bead
  `ytt-58325cdf`). Fetches captions for one known-good video from wherever it
  runs and prints a JSON report — `verdict: "ok"` vs `"ip_blocked"` (exit
  0/1), with the ipinfo egress classification as context and a UTC `ran_at`
  stamp for evidence. The lightweight vehicle for the plan's residential-
  egress Proof Obligation: runnable in-cluster via `kubectl exec` (or any
  one-shot pod/Argo step) without deploying the long-running canary
  Deployment. The long-running `ytt canary` loop and its `:8081` metrics are
  unchanged.

### Fixed

- **`YTT_MAX_CONCURRENT_WHISPER` is now actually enforced** (bead
  `ytt-7dc271a6`). The semaphore existed (`ConcurrencyState.whisper_sem`) and
  the knob was documented, but no code ever acquired it — any number of
  "concurrent" jobs could hit the shared CPU Whisper service at once. New
  jobs now run under a per-job reservation
  (`server._run_whisper_job_bounded`): the slot is held from the moment the
  job leaves `pending` until it reaches `done` or `error` — released on
  success and failure alike, and on task cancellation — so a failed
  transcription cannot wedge the shared service. Jobs beyond the cap queue as
  `pending` (they have already paid their `YTT_WHISPER_JOBS_PER_HOUR` slot).
  Also: a quota charge whose `get_or_create` unexpectedly fails is refunded
  (release-on-failure — a server fault no longer permanently spends a
  caller's slot), surfaced in the usual structured error shape instead of an
  escaping exception.

## [0.2.16] — 2026-09-17

### Fixed

- **Registry-note correction, and the README-advertised image actually
  published** (follow-up on the 0.2.15 reconciliation, bead
  `ytt-8efb9b9d`). That entry claimed `ronaldraygun/ytt:0.2.14` was never
  built and that 0.2.15 was the first ronaldraygun/ytt tag CI builds and
  pushes — both wrong: Docker Hub shows `0.2.13` published 2026-09-16
  14:19 UTC and `0.2.14` at 18:26 UTC (the 0.2.14 push passed the
  `resolve-version` VERSION-bump gate and CI built it minutes later).
  The gate does, by design, fail any later master push that doesn't bump
  VERSION — which left 0.2.15 itself without an image: master moved past
  its bump commit, and the kaniko git context resolves only
  `refs/heads/<branch>`, so the tagged commit is not buildable that way
  either. 0.2.15 therefore stands as a metadata-only release (its tag and
  CHANGELOG entry are accurate; it simply has no image), and this bump
  exists so CI builds the image the README advertises. No code changes.

## [0.2.15] — 2026-09-16

### Added

- **Single-replica invariant enforcement** (`ytt/singleton.py`,
  `tests/unit/test_single_replica.py`, `tests/unit/test_singleton.py`).
  `serve()` now takes an exclusive `flock` on
  `<cache_dir>/.ytt-singleton.lock` — held for the process lifetime — so a
  second instance aimed at the same cache dir (scale-out, or a stray
  process) fails loudly (`Single-replica invariant violated`, exit 1 →
  CrashLoopBackOff) instead of silently splitting the in-process state
  (cache byte-counter, single-flight map, Whisper job registry, rate-limit
  buckets) that is only correct at `replicas: 1`. Every Deployment under
  `deploy/k8s/` is asserted to pin `replicas: 1` explicitly plus
  `strategy: Recreate` (load-bearing: the default `RollingUpdate` has
  `maxSurge >= 1`, which briefly runs two live servers on split state
  during every deploy). Rationale table in `docs/notes/single-replica.md`.
- **deploy/ ↔ declarative-config mirror-parity test**
  (`tests/unit/test_deploy_parity.py`). Byte-compares every file under
  `deploy/k8s/` against its `declarative-config` counterpart (and requires
  the mirrored tree to match as a set), so the full-revision drift 0.2.14
  fixed (bead `ytt-15205fb4`) fails CI instead of being rediscovered by
  hand. Skips where no declarative-config checkout exists (CI image
  builds).

### Fixed

- **Release-history reconciliation for 0.2.13–0.2.14** (bead
  `ytt-8efb9b9d`, continuing `ytt-a28cf823` past 0.2.12). The 0.2.13 and
  0.2.14 CHANGELOG entries were audited against their release commits
  (`c0d0d8e`, `2b8f5f6`) and are accurate apart from the Docker Hub clause
  corrected below, but three pieces of release metadata were missing:
  the `v0.2.14` annotated tag did not exist (the README-advertised release
  was unresolvable by tag) — backfilled at its VERSION-bump commit
  `2b8f5f6`; the CHANGELOG compare-link refs stopped at 0.2.13 (`[0.2.14]`
  had no definition, `[Unreleased]` still compared against `v0.2.13`);
  and `uv.lock` was left at 0.2.13 by the 0.2.14 commit (which bumped
  `pyproject.toml` without a lock regen — `uv lock --check` failed on a
  clean checkout). One at-tag fact recorded on re-audit: the tagged tree
  of `v0.2.13` itself carries stale metadata — `pyproject.toml` still
  0.2.12, `ytt.__version__` still 0.2.1 (stale since 0.2.1, and
  advertised by the running server), `uv.lock` still 0.2.1 — because the
  reconciliation that fixed them (`a0f2cfe`) is the commit *after* the
  tagged release commit `c0d0d8e`, and the tag is immutable history. A
  clean checkout of `v0.2.13` therefore reports those stale values; 0.2.14
  was the first release whose tagged tree had matching `pyproject.toml`
  and `__version__` (its own `uv.lock` lag is the fix above).
  (This entry's registry note was corrected in 0.2.16:
  `ronaldraygun/ytt:0.2.13` and `:0.2.14` were in fact published on
  2026-09-16, and 0.2.15 itself shipped without an image — see the 0.2.16
  entry.)
- The 0.2.14 entry's Docker Hub wording ("the Docker Hub repo must stay
  public") wrongly implied the visibility flip had already happened; it is
  a pending operator step (anonymous pulls 401 until flipped — the same
  correction commit `5632ca7` made to `plan.md`,
  `docs/usage/deploy-ardenone.md`, and the README's 401 pointer, which had
  not reached this file). The 0.2.14 entry now states the flip is pending.

## [0.2.14] — 2026-09-16

### Changed

- **Public image publishing restored: the documented image is now
  `ronaldraygun/ytt` on Docker Hub, replacing the never-published
  `ghcr.io/jedarden/ytt`.** The README quick-start and self-hosting guide
  pointed at a GHCR image that does not exist, while CI pushed
  `ronaldraygun/ytt:<version>` to a *private* Docker Hub repo — so the
  quick-start `docker run` was denied to every external user (bead
  `ytt-15205fb4`). All public docs now reference `ronaldraygun/ytt:<version>`;
  the Docker Hub repo must be **public** for those pulls to work (Hub-UI
  visibility flip — the Hub API has no visibility-change endpoint and the
  stored PAT is read-scoped; procedure in `deploy/DEPLOY-CHECKLIST.md`;
  **the flip is a pending operator step — anonymous pulls still 401 until
  it is done**, see the 0.2.15 correction). The registry decision is
  recorded as an addendum in `docs/plan/plan.md` ("Image publishing").

### Fixed

- **`deploy/` had drifted a full revision behind the applied state and is
  now an exact mirror of the applied `declarative-config` manifests** (same
  layout, same filenames, drift check in `deploy/README.md`). The in-repo
  `ytt-build` WorkflowTemplate still described the GHCR push + sed
  auto-bump flow; the in-repo deployment pinned `ghcr.io/jedarden/ytt:0.1.0`;
  the applied-but-unmirrored `oauth-state-pvc.yml` and `ytt-externalsecret.yml`
  were missing; and the never-applied Phase-9 scaffolding
  (`canary-deployment.yaml`, `test-deployment.yaml`, the old Google-era
  `external-secret.yaml`) is gone. `DEPLOY-CHECKLIST.md` was rewritten to
  the actual release SOP (`bao`/OpenBao paths, Authentik subject env,
  Docker Hub visibility, no CI auto-bump).

## [0.2.13] — 2026-09-08

### Fixed

- **Root cause of ytt still cycling through full OAuth+DCR re-auth every
  20-90min in production despite 0.2.12's 1wk/12wk token-lifetime fix.**
  ytt's own AS metadata (`scopes_supported`, driven by `required_scopes`/
  `update_default_scopes` in `build_auth_provider()`) advertised only
  `["openid", "email"]` -- Claude only appends `offline_access` to its
  authorize request when the AS advertises it, so ytt never requested it,
  Authentik's token response therefore never included a `refresh_token`,
  and `OAuthProxy.exchange_authorization_code()` silently clamps
  `fastmcp_access_expires_in` back down to Authentik's raw ~5min
  `access_token_validity` whenever `idp_tokens` lacks a `refresh_token`
  (see `proxy.py`: `if not idp_tokens.get("refresh_token"): ...= min(...)`)
  -- regardless of the `fastmcp_access_token_expiry_seconds=1wk` set at
  construction. No FastMCP refresh token was issued either, so the client
  had nothing to silently refresh with and fell straight to a full
  re-auth (fresh DCR client_id every time) on every expiry. Confirmed live
  via `FASTMCP_LOG_LEVEL=DEBUG`-adjacent log capture 2026-09-08: repeated
  `/register`+`/authorize`+`/token` cycles, `scope=openid+email` only, zero
  intervening refresh-grant `POST /token` calls. Fix: add `offline_access`
  to `required_scopes`/`update_default_scopes` so it's advertised in ytt's
  own AS metadata (per `docs/research/mcp-oauth-authentication.md`'s
  "offline_access placement" guidance -- AS metadata, not resource
  metadata) and requested from Authentik on the upstream authorize call.
  Requires the matching `scope-offline_access` property mapping on
  Authentik's `provider-ytt` (declarative-config), or Authentik may not
  actually grant/return the scope even though it's requested.

## [0.2.12] — 2026-08-15

### Changed

- **Claude-held access token lifetime extended from Authentik's 5-minute
  default to 1 week, refresh window extended to 12 weeks.** Root cause:
  `build_auth_provider()` never set `fastmcp_access_token_expiry_seconds`,
  so the FastMCP-issued token Claude actually holds inherited Authentik's
  raw `access_token_validity` (5 minutes, org default) on every issuance
  *and* every refresh, instead of using `OAuthProxy`'s built-in
  transparent-upstream-refresh design to decouple the two. Whether the
  resulting frequent silent refreshes were actually invisible to the
  client, or occasionally surfaced as a full re-auth prompt, wasn't
  isolated -- fixing the token lifetime directly sidesteps the question.
  Requires a matching `refresh_token_validity: weeks=12` on the ytt
  OAuth2Provider in declarative-config (Authentik's own 30-day default
  refresh-token lifetime would otherwise still cap the chain regardless of
  what ytt's own refresh JWT claims).

## [0.2.11] — 2026-08-15

### Fixed

- **Root cause of "OAuth completes, tools/list handler runs, but the
  client sees zero tools," found via the 0.2.10 diagnostic logging:**
  `check_subject_auth` required the token's `email_verified` claim to be
  truthy, on every single call, unconditionally -- but this Authentik
  instance's default "scope-email" mapping (`goauthentik default OAuth
  Mapping: OpenID 'email'`, shared by every OAuth2 provider on
  sso.ardenone.com) hardcodes `"email_verified": False` in its expression
  for every account, with no per-user override. It has never once been
  able to return `True` since the Authentik migration, independent of the
  YTT_ALLOWED_SUBJECTS domain fix, independent of everything else
  investigated today. Dropped the `email_verified` requirement --
  meaningless against this IdP's local, operator-created, non-federated
  accounts, and access to the ytt application itself is already gated by
  Authentik's `platform-admins` group policy. See `ytt/authz.py`'s module
  docstring for the full reasoning.

### Removed

- The 0.2.10 temporary diagnostic logging in `check_subject_auth`, now
  that its job (finding the above) is done.

## [0.2.10] — 2026-08-15

### Added

- **Temporary diagnostic logging in `check_subject_auth`** (`ytt/authz.py`).
  Investigating "OAuth completes, tools/list handler runs, but no tools are
  visible client-side" after the `YTT_ALLOWED_SUBJECTS` jedarden.com fix.
  Domain match looked correct end-to-end from the access logs, but
  `check_subject_auth` has no logging of its own, so DEBUG-level FastMCP
  logs can't show whether `email`/`email_verified` actually land on
  `ctx.token.claims` (vs., e.g., ending up nested under an
  `upstream_claims` key per `OAuthProxy._validate_upstream_token`'s
  claim-propagation code, which this instance's OIDC setup exercises via
  `verify_id_token=True`). Logs claim key names and boolean
  present/verified/allowed flags only -- never the email value itself
  (already a redacted field name in `ytt/observability.py`). Remove once
  resolved.

## [0.2.9] — 2026-08-15

### Fixed

- **0.2.8's `verify_id_token=True` fix was incomplete -- `invalid_token`
  persisted, identically, on the ID token this time.** Root cause:
  Authentik signs *every* OAuth2Provider's tokens on this instance --
  access token AND id token alike -- with HS256 (symmetric, keyed by the
  client_secret), confirmed by comparing against OpenBao's own working
  provider (identical `id_token_signing_alg_values_supported: ["HS256"]`,
  identically empty JWKS `{}`). This is a normal, spec-compliant OIDC
  configuration for confidential clients, not a misconfiguration --
  OpenBao's OIDC client just never tries to verify the signature via
  JWKS at all. `OIDCProxy.get_token_verifier()` unconditionally builds a
  JWKS-based `JWTVerifier` with no path for symmetric verification, so
  it could never work against this IdP regardless of which token
  (access or id) it pointed at. Fixed by constructing our own
  `JWTVerifier(public_key=<client_secret>, algorithm="HS256", ...)` --
  it explicitly supports shared-secret verification -- and passing it as
  `token_verifier=` to bypass the broken auto-construction.

## [0.2.8] — 2026-08-15

### Fixed

- **Every real connector auth 401'd on the very first tool call, after a
  fully successful OAuth dance.** Login, consent, Authentik token
  exchange, and FastMCP's own self-issued token all completed with
  200s -- then `POST /ytt` rejected the token as `invalid_token`
  immediately, same pod, no restart in between. Root cause (confirmed
  via `FASTMCP_LOG_LEVEL=DEBUG` live logs, not guessed): Authentik
  signs access tokens with HS256 (symmetric) per its own discovery
  document, so its JWKS endpoint correctly has no keys for it -- a
  shared HS256 secret can never be published there. `OIDCProxy`'s
  default behavior verifies the upstream *access* token via JWKS
  (assuming an asymmetric algorithm), which fails with "No keys found
  in JWKS" every time. Fixed with `verify_id_token=True` in
  `ytt/auth.py` -- only the ID token is meant to be independently
  verifiable this way. This also resolves 0.2.6's open question about
  where `email`/`email_verified` land (the ID token, not the access
  token).

## [0.2.7] — 2026-08-15

### Fixed

- **Authentik rejected every real connector login.** `OIDCProxy`'s
  default `forward_resource=True` relayed the RFC 8707 `resource`
  parameter Claude sends on `/authorize` through to Authentik's
  `/application/o/authorize/` call, which Authentik rejects outright
  (`error=invalid_request`, "The request is otherwise malformed") —
  the login never even reached Authentik's page. 0.2.6's ADR-003 note
  had ruled this out via static analysis of a FastMCP source comment
  that turned out not to describe Claude's actual behavior; confirmed
  live via Traefik access logs and fixed with `forward_resource=False`
  in `ytt/auth.py`.

## [0.2.6] — 2026-08-15

### Changed

- **OAuth federation: Google → self-hosted Authentik (ADR-003).**
  `ytt.auth.YttGoogleProvider` (FastMCP's `GoogleProvider`) replaced with
  `YttOIDCProvider` (FastMCP's generic `OIDCProxy`), which discovers its
  upstream endpoints from Authentik's per-application
  `.well-known/openid-configuration` document instead of hardcoding
  Google's. ytt now has its own Authentik application/client instead of
  sharing ibkr-mcp's GCP OAuth app. `YTT_OAUTH_CLIENT_ID`/
  `YTT_OAUTH_CLIENT_SECRET` now hold Authentik-issued values, sourced from
  a new consolidated OpenBao path (`ardenone-cluster/ytt/oauth`, keys
  `client_id`/`client_secret`) instead of the old two-path
  `oauth-client-id`/`oauth-client-secret` shape. See `docs/plan/plan.md`
  ADR-003 and `ytt/auth.py`'s module docstring.

### Fixed

- **This release was built by CI but never actually deployed for hours**
  — `declarative-config`'s `deployment.yml` kept referencing `0.2.5`
  after this code landed, so the live pod kept running the old
  Google-federated build. Caught when a real connector re-auth attempt
  redirected to Google Workspace instead of Authentik; confirmed via
  Traefik's access log (`/ytt/authorize` carrying
  `scope=openid+https://www.googleapis.com/auth/userinfo.email`, a
  Google-specific scope URI only the old provider produces).

## [0.2.5] — 2026-07-18

### Fixed

- **Caption-only extraction still failed for DRM-protected videos.**
  `allow_unplayable_formats` (0.2.4) cleared the DRM abort, but
  `extract_info()` still runs format selection even with
  `download=False`/`skip_download`, and with all-DRM formats it raised
  "Requested format is not available" — again bailing before ytt reads
  the caption tracks. We only ever want subtitles, so set
  `ignore_no_formats_error` to warn-and-continue; yt-dlp returns the info
  dict with `subtitles`/`automatic_captions` populated.

## [0.2.4] — 2026-07-17

### Fixed

- **DRM-protected videos falsely reported "no captions."** YouTube now
  DRM-protects many ordinary uploads (SABR). yt-dlp aborted `extract_info()`
  with "This video is DRM protected" (unless `allow_unplayable_formats`
  is set), which also killed caption retrieval even though caption tracks
  aren't DRM-encrypted and the caption path never downloads media
  (`skip_download=True`). Set `allow_unplayable_formats` so extraction
  proceeds past the DRM check.

## [0.2.3] — 2026-07-16

### Fixed

- **YouTube extraction failed systematically: every video returned "unavailable."**
  The 14-month-stale yt-dlp pin (2025.5.22) could no longer extract from
  YouTube's current player — every fetch returned error code 152, which ytt
  mislabeled as "no captions found" and turned into doomed Whisper ASR
  fallbacks (audio download failed the same way). Bumped to yt-dlp 2026.7.4
  to match today's YouTube player.

## [0.2.2] — 2026-07-16

### Changed

- **Authorization allowlist now supports case-insensitive matching and @domain
  wildcards.** `check_subject_auth` did a case-SENSITIVE exact match on the
  Google-verified email, and only exact emails were allowlistable. Two
  consequences hit in production: (1) an email returned as
  Me@jedcabanero.com failed against a lowercase allowlist entry, and (2)
  each Claude client that granted OAuth with a different Google account
  needed its exact email enumerated (both surfaced as "This connector has no
  tools available" — AuthMiddleware silently filters every tool a caller
  isn't authorized for, so `tools/list` returns empty). New `subject_allowed()`
  is case-insensitive and supports '@domain' entries (e.g. `@jedcabanero.com`)
  that admit any address in that exact domain (the leading '@' anchors the
  match so `evil-jedcabanero.com` and `sub.jedcabanero.com` do NOT match).
  Safe because callers gate on a Google-verified email.

## [0.2.1] — 2026-07-16

### Fixed

- **DCR/OAuth operational routes 404'd behind the path prefix.** The MCP
  SDK mounts `/register`, `/authorize`, `/token`, `/revoke`, and
  OAuthProxy's upstream-IdP callback (`/auth/callback`) at hardcoded bare
  paths (`mcp.server.auth.routes.AUTHORIZATION_PATH` etc. are not
  issuer-path-aware), even though the metadata these routes advertise
  (`registration_endpoint`, `authorization_endpoint`, ...) correctly uses
  the path-bearing issuer URL (`https://mcp.ardenone.com/ytt`). ytt's
  IngressRoute only forwards `PathPrefix("/ytt")` (plus the two well-known
  suffixes) to this service, so Claude's connector-add flow 404'd on
  `POST /ytt/register` ("Couldn't register with YouTube Transcript's
  sign-in service"). DCR was never exercised under the old
  InMemoryOAuthProvider design (had it disabled), so this predates and is
  independent of the 0.2.0 auth-provider swap — it just never got hit until
  now. `YttGoogleProvider.get_routes()` now mounts every non-well-known
  operational route a second time under the issuer path.

## [0.2.0] — 2026-07-15

### Fixed

- **Security: auth was not actually enforced on the tools.** `ytt/auth.py`
  used FastMCP's `InMemoryOAuthProvider` — an explicit test/demo provider
  (its own docstring: "simulates user authorization") that auto-approves any
  caller with no login step and issues opaque tokens with no `sub`/`email`
  claim. `YTT_ALLOWED_SUBJECTS` was also never checked on the actual tool
  calls — the allowlist check existed but was wired only into the
  `/admin/egress` diagnostic route. Combined with public (`websecure`)
  exposure, this meant any Claude user who discovered
  `https://mcp.ardenone.com/ytt` could call the transcript tools regardless
  of the allowlist. Replaced with FastMCP's `GoogleProvider` (federates to
  Google OAuth, same identity model as ibkr-mcp) and wired the allowlist
  check into global `AuthMiddleware`, covering every tool call.
- `/admin/egress` also independently 403'd every caller, including allowed
  ones, and 500'd instead of returning a clean 401/403 for bad tokens — it
  decoded the (non-JWT) bearer token as a JWT, passed the wrong type to the
  allowlist check, and inverted the raise/return contract of that check.
  Rewritten to use `fastmcp.server.dependencies.get_access_token()`.
- CI (`ytt-build`) never actually ran — it cloned from
  `github.com/jedarden/ytt`, which didn't exist (Forgejo push-mirror was
  never set up), and pushed to `ghcr.io/jedarden/ytt`, not the
  `ronaldraygun/ytt` Docker Hub image actually referenced by the deployment
  manifest. Set up the Forgejo → GitHub push-mirror and repointed CI at
  Docker Hub with semver `VERSION`-file tagging, matching every other app in
  this fleet.
- `YTT_CACHE_MAX_BYTES=2Gi` exceeded the PVC's actual usable capacity
  (ext4 reserved blocks report less than the nominal request), tripping the
  app's own startup validation on every boot — `CrashLoopBackOff` for
  5d20h/1636 restarts before this fix.

## [0.1.0] — 2026-07-06

Initial release.

### Added

- `get_youtube_transcript` MCP tool — fetches transcript for any YouTube URL
  (watch, youtu.be, /shorts/, /live/, bare video ID, &list= stripped).
- `get_transcript_job` MCP tool — polls Whisper ASR job; returns transcript when done.
- Rolling-caption dedup — eliminates the #1 silent bug in auto-captions (duplicate
  text in rolling tracks).
- Chunk pagination — inline for short videos; `next_cursor` + loud PARTIAL prefix
  for long videos.
- `start`/`end`/`query` transcript filtering.
- Per-subject OAuth 2.1 + PKCE with audience-bound token validation.
- Subject allowlist (`YTT_ALLOWED_SUBJECTS`) — fail-closed (empty = deny all).
- Per-subject rate limiting (token bucket) + Whisper quota.
- Flat-file LRU transcript cache with configurable byte cap and ENOSPC degrade.
- Single-flight dedup — concurrent same-video requests share one yt-dlp call.
- Whisper ASR fallback — async job FSM with ETA, TTL GC, scratch audio cleanup.
- `ytt selftest` egress probe — reports IP/ASN/org/is_residential.
- `ytt selftest --show-sub` — discovers the OAuth `sub` for allowlist setup.
- `ytt test [--unit|--integration]` — test runner with JSON summary.
- Prometheus metrics (`/ytt/metrics`) with labelled counters/gauges/histograms.
- `PrometheusRule` alerts: IP burned, Whisper down, cache undersized, egress changed, canary failed.
- Canary Deployment — yt-dlp caption probe every 10 min, metrics on :8081.
- `structlog` JSON logging with redaction filter (tokens, subjects, transcript bodies).
- `/ytt/admin/egress` — auth-gated egress diagnostics.
- K8s manifests for `ardenone-cluster` co-hosted with `ibkr-mcp` (additive, do-no-harm).
- Argo Workflows CI (`ytt-build`) — pytest gate + kaniko build + GHCR push + tag bump.
- Property-based tests for Invariants 1–6 (Hypothesis).
- Integration test harness for 22 in-cluster scenarios.
- Public GHCR image: `ghcr.io/jedarden/ytt:0.1.0`.

[Unreleased]: https://github.com/jedarden/ytt/compare/v0.2.16...HEAD
[0.2.16]: https://github.com/jedarden/ytt/compare/v0.2.15...v0.2.16
[0.2.15]: https://github.com/jedarden/ytt/compare/v0.2.14...v0.2.15
[0.2.14]: https://github.com/jedarden/ytt/compare/v0.2.13...v0.2.14
[0.2.13]: https://github.com/jedarden/ytt/compare/v0.2.12...v0.2.13
[0.2.12]: https://github.com/jedarden/ytt/compare/v0.2.11...v0.2.12
[0.2.11]: https://github.com/jedarden/ytt/compare/v0.2.10...v0.2.11
[0.2.10]: https://github.com/jedarden/ytt/compare/v0.2.9...v0.2.10
[0.2.9]: https://github.com/jedarden/ytt/compare/v0.2.8...v0.2.9
[0.2.8]: https://github.com/jedarden/ytt/compare/v0.2.7...v0.2.8
[0.2.7]: https://github.com/jedarden/ytt/compare/v0.2.6...v0.2.7
[0.2.6]: https://github.com/jedarden/ytt/compare/v0.2.5...v0.2.6
[0.2.5]: https://github.com/jedarden/ytt/compare/v0.2.4...v0.2.5
[0.2.4]: https://github.com/jedarden/ytt/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/jedarden/ytt/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/jedarden/ytt/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/jedarden/ytt/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/jedarden/ytt/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/jedarden/ytt/releases/tag/v0.1.0
