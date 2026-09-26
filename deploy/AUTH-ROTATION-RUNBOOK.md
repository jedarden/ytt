# ytt Operator Runbook — auth credential & signing-key rotation

Rotation procedures for everything ytt authenticates **with** or **against**:
the upstream OIDC client credential, the FastMCP token-signing key (and the
JWKS verification posture around it), and the subject allowlist. What each
rotation costs, the exact sequencing on a single replica, how old and new
tokens behave in the window, what rollback can and cannot undo, and the
secret-safe verification at every step.

Related docs:

| Doc | Covers |
|---|---|
| [RUNBOOK.md](RUNBOOK.md) | The Recreate swap this runbook's restarts ride on, the GitOps write path, forbidden kubectl, rollback mechanics |
| [OAUTH-STATE-RUNBOOK.md](OAUTH-STATE-RUNBOOK.md) | The state a signing-key rotation orphans (fingerprint directories), backup/restore under a rotated key |
| [DEPLOY-CHECKLIST.md](DEPLOY-CHECKLIST.md) | The condensed OpenBao write recipe and allowlist env-var notes this runbook expands into a full procedure |
| [docs/notes/auth.md](../docs/notes/auth.md) | Why these key families exist, the fail-closed failure-mode table, the conformance pins |
| [docs/notes/single-replica.md](../docs/notes/single-replica.md) | Why every step here is a whole-pod restart, never a scale or rolling action |

Every behavioral claim below is pinned by a test in
`tests/unit/test_oauth_conformance.py` or its siblings (§10) — if an
implementation change makes a claim false, the pin fails on purpose.

## 1. What there is to rotate, and where each thing lives

| Thing | What it does | Where it is stored | How it reaches the processes |
|---|---|---|---|
| `client_id` / `client_secret` | Authentik's OAuth2 client credential for ytt — what ytt **presents** and what Authentik's blueprint **expects** | **OpenBao** `secret/ardenone-cluster/ytt/oauth` (instance `openbao-v2`, `cas_required`) — one path, two consumers | Two ExternalSecrets (`refreshInterval: 1h`): `ytt-externalsecret.yml` → Secret `ytt-secrets` (ns `ytt`) → env `YTT_OAUTH_CLIENT_ID`/`YTT_OAUTH_CLIENT_SECRET`; `authentik-oidc-clients-externalsecret.yml` → Secret `authentik-oidc-clients` (ns `authentik`) → the authentik pod's env via `secretKeyRef`, consumed by the blueprint's `!Env` tags |
| Upstream id_token verification key | The same `client_secret` used **as the HS256 verifier** for Authentik-signed id_tokens (the reference IdP publishes an empty JWKS `{}`) | Derived in memory at startup from `YTT_OAUTH_CLIENT_SECRET` | `UpstreamIdTokenVerifier` (`ytt/auth.py`) — holds **exactly one** static key |
| FastMCP token signing key | Signs the access **and** refresh tokens Claude actually presents | Derived (HKDF, fixed salt) from the client secret unless `YTT_JWT_SIGNING_SECRET` is set — **never stored anywhere** | In memory only; deterministic, so tokens survive restarts |
| OAuth-state storage encryption key | Encrypts every entry on the `ytt-oauth-state` PVC | Derived from the signing key; its `sha256[:12]` **is** the fingerprint directory name on `/state` | `/state/oauth-proxy/<fingerprint>/…` |
| Subject allowlist | Authorization — which subjects may call tools | **Not a credential, not in OpenBao** — a plain env var in `deployment.yml` | `YTT_ALLOWED_SUBJECTS` at container start |

The derivation chain (verified against the installed `fastmcp` 3.4.2,
`server/auth/oauth_proxy/proxy.py`):

```
client_secret
  → derive_jwt_key(salt="fastmcp-jwt-signing-key")         # the signing key
      → derive_jwt_key(salt="fastmcp-storage-encryption-key")  # storage key
          → sha256(...)[:12]                               # fingerprint dir
```

An explicit `YTT_JWT_SIGNING_SECRET` replaces the first step's *input* (it is
itself passed through the same HKDF as low-entropy material). The one-sentence
summary an operator must be able to recite: **rotating the client secret
rotates every key family at once — the verifier, the token signing key, and
the storage encryption key — which is why the cost model in §2 is what it
is.**

Both consumers reading **one** OpenBao path is what makes rotation a single
write instead of two coordinated edits that can drift (`ytt-secret.yml.template`
documents the same for initial provisioning).

## 2. Old/new token behavior — what rotation actually does

- **There is no dual-key acceptance window.** The upstream verifier and the
  token issuer each hold exactly one key (`TestUpstreamSecretRotation.
  test_verifier_holds_exactly_one_static_key`). The moment the new value is
  live in a pod's env — i.e. at that pod's restart — old-signature material
  fails. Nothing cached, nothing to flush, and no fallback that ever accepts
  the old signature (`TestYTTSignedTokenRotation`).
- **Rotating the client secret is a full logout.** The derived signing key
  changes with it, so every FastMCP access *and* refresh token dies at the
  signature check, and every connected client re-runs the connector flow
  (discovery → DCR → interactive Authentik login → new tokens). Pinned by
  `TestYTTSignedTokenRotation.test_client_secret_rotation_rotates_the_derived_signing_key_too`.
- **The state volume is orphaned, not destroyed.** The new storage key derives
  a new fingerprint directory; the old tree stays on `/state`, inert and
  unreadable. Do not delete it "for hygiene" — see
  [OAUTH-STATE-RUNBOOK.md](OAUTH-STATE-RUNBOOK.md) §4.3.
- **Between the two sides flipping, new logins fail — closed.** A login whose
  id_token is signed under one secret while ytt verifies against the other
  gets a 401, never a pass. The window is fail-closed by design; there is no
  configuration that makes it fail open.
- **Decoupled mode** (`YTT_JWT_SIGNING_SECRET` set): a client-secret rotation
  then leaves local sessions *alive* — the FastMCP tokens keep verifying; the
  upstream tokens inside the state volume die only at the next transparent
  upstream refresh (FastMCP session tokens last up to 1 week; the real
  ceiling is Authentik's own refresh-token validity). Setting or changing
  `YTT_JWT_SIGNING_SECRET` itself remains a full logout **plus** state
  orphaning, because both derived keys change with it.
- **The JWKS/RS256 path, if an IdP switch ever makes it live**, has bounded
  staleness instead of immediacy: a key removed from the live JWKS keeps
  validating for at most the **1 h** cache TTL
  (`TestJWKSPathFailClosed.test_rotated_out_key_rejected_once_cache_expires`,
  `.test_cache_ttl_is_one_hour`). That is the price of the cache on that path
  (auth.md §"Validation is offline"); see §5.

## 3. Rotation procedure — upstream client secret

The common rotation (hygiene or suspected compromise). Expected cost, stated
up front so nobody is surprised mid-procedure: **every user and client
re-authenticates**, one Recreate swap each for the ytt and authentik pods
(§3.3), and a login-dead window between the two flips (§2, fail-closed,
self-healing once both pods run the new secret).

### 3.1 Write the new secret into OpenBao (secret-safe)

Do it in a maintenance window. The value travels by **pipe only** — never as
a command-line argument, never through a terminal, never into a file that
outlives the call. Use the **write-only** provisioning identity; it cannot
read what is already stored, which is the point.

```bash
# Current version (0 if the path does not exist). The provisioning identity
# reads metadata precisely so this CAS step needs no read access:
V="$(bao-as openbao-v2-provision bao kv metadata get -format=json \
      secret/ardenone-cluster/ytt/oauth | jq .data.current_version)"

# Generate and write in one pipeline — the plaintext never exists as a
# literal anywhere (not in argv, not in the transcript, not on disk):
{ printf '{"client_id":"<ytt client_id — not a credential>","client_secret":"';
  openssl rand -hex 32 | tr -d '\n';
  printf '"}'; } \
  | bao-as openbao-v2-provision bao kv put -cas="$V" \
      secret/ardenone-cluster/ytt/oauth -
```

- **The new version must carry both properties.** Each ExternalSecret
  `remoteRef`s `client_id` and `client_secret` from this one path; a version
  with only one property breaks the other. Keep the `client_id` stable — it
  is not a credential and appears in redirect URLs anyway; rotate the secret.
- The `@payload.json` form from DEPLOY-CHECKLIST §OpenBao is equivalent if
  you prefer a file: mode `600`, deleted immediately after the call.
- The mount is `cas_required` — `-cas="$V"` is not optional. A concurrent
  write without it is silently overwritten.
- **Verify the write by metadata, never by reading the value back:**
  `current_version` must now be `$V + 1`. A bare `bao kv get` of this path
  dumps the plaintext into the terminal and the transcript — never do it.

### 3.2 Converge the two consumers

Both ExternalSecrets refresh within their `refreshInterval: 1h`. Check sync
status read-only through the credential-free proxy:

```bash
KS="kubectl --server=http://traefik-ardenone-cluster:8001"
$KS get externalsecret -n ytt ytt-secrets -o wide          # SecretSynced=True
$KS get externalsecret -n authentik authentik-oidc-clients -o wide
```

To converge before the hour mark, change an annotation on the ExternalSecret
in `declarative-config` and push — any spec/annotation change triggers an
immediate ESO reconcile (the observed house pattern is a timestamped
`force-sync-*` annotation, e.g. on `authentik-oidc-clients`). Still never a
live `kubectl annotate`: `selfHeal` reverts it.

### 3.3 Restart sequencing (single replica, two pods)

**Both pods need a restart, not just ytt's.** Env vars are read at container
start: ytt presents the credential from its env, and the authentik pod's
blueprint resolves `!Env` from *its* env. A k8s Secret refresh alone changes
nothing in either running process.

The house restart is the GitOps door, never kubectl (RUNBOOK §7):

1. Bump `kubectl.kubernetes.io/restartedAt` on the pod template of
   `deployment.yml` (ns `ytt`) **and** the authentik deployment in
   `declarative-config`, commit, push. ArgoCD syncs; `Recreate` performs each
   kill-then-start swap.
2. **Order does not matter; the window does.** Whichever pod flips first,
   logins fail closed until the second one follows (§2). Existing FastMCP
   sessions die at ytt's restart regardless of order.
3. Each swap is the standard single-replica outage: no pod for the gap +
   pull, in-flight Whisper jobs lost (RUNBOOK §2.1) — another reason §3.1
   belongs in a quiet window.

### 3.4 Post-rotation verification (run in order, all secret-safe)

1. **Pod shape** — one fresh `1/1` ytt pod on the pinned tag:
   `$KS get pods -n ytt`.
2. **Health** — `curl -s https://mcp.ardenone.com/ytt/health` →
   `{"status": "ok"}`. Health and metrics are unauthenticated by design
   (`TestSubjectAllowlistCoverage.test_public_health_and_metrics_stay_open`),
   so this works while every authenticated surface is still logged out.
3. **OAuth metadata unchanged** — the RFC 9728 documents derive from
   `YTT_PUBLIC_URL`, not from any rotated value:
   `curl -s https://mcp.ardenone.com/ytt/.well-known/oauth-protected-resource`
   must be byte-identical to before (same check shape as RUNBOOK §3 step 3's
   ibkr hash comparison).
4. **A real client re-logins and calls a tool** — the actual end-to-end
   proof. It exercises the new secret on both sides (login completes ⇒
   Authentik and ytt agree) **and** the allowlist (the session resolves
   tools rather than being silently filtered to zero — the exact failure
   mode recorded in `deployment.yml`'s allowlist comment).
5. **The orphan is expected** — `ls /state/oauth-proxy` (operator exec; the
   credential-free proxy cannot exec, RUNBOOK §7) shows the old fingerprint
   directory beside the new one. Leave it.
6. **Record it** — OpenBao version number, ExternalSecret sync times, the
   `restartedAt` values, pod names — on the bead or ticket for the
   operation. A rotation nobody recorded is indistinguishable from a mystery
   (OAUTH-STATE-RUNBOOK §7.6).

## 4. Rotating the ytt signing key independently

`YTT_JWT_SIGNING_SECRET` is the **global logout lever that leaves the IdP
credential untouched** (auth.md §"Operator recovery runbook"): set or change
it and roll only the **ytt** pod — Authentik is not involved, upstream tokens
in the state volume die at the next upstream refresh, and no future
client-secret rotation will log anyone out on the ytt side again.

- It is unset in the reference deployment. If you set it, it is a real
  credential: give it its own OpenBao property and a `remoteRef` in
  `ytt-externalsecret.yml` + a `secretKeyRef` in `deployment.yml` — manifests
  carry references, never values. Generate with `openssl rand -base64 32` by
  the same pipe recipe as §3.1 (fastmcp warns under 12 chars; it is HKDF'd
  either way — entropy is free, use it).
- **First set = one-time state orphaning** (both derived keys change),
  OAUTH-STATE-RUNBOOK §1.1. After it is set, client-secret rotations (§3) no
  longer orphan the state and no longer log sessions out — the trade is
  worth it before the second rotation, not after the fleet is logged in.

## 5. The JWKS path — an RS256 IdP, present or future

Today the reference IdP (Authentik, HS256) publishes an **empty JWKS**, ytt
never fetches one, and validation is offline-symmetric — §3 is the whole
rotation story. The JWKS path (`TestJWKSPathFailClosed`) is specified and
pinned for the day an RS256 IdP arrives; rotation then happens **IdP-side**,
and ytt's pinned behavior defines the IdP operator's obligations:

| Event at the IdP | ytt behavior | What the IdP operator must do |
|---|---|---|
| New key published alongside the old | New tokens verify once fetched; cached old keys keep working | Standard JWKS overlap — leave the old key published |
| Old key removed from the live JWKS | Still accepted **up to the 1 h cache TTL**, then rejected | Keep the old key live **≥ 1 h** after the new one, or in-flight sessions die inside the window |
| JWKS endpoint outage, cold cache | Reject (fail closed) | Restore the endpoint |
| Empty JWKS / unknown `kid` | Reject (fail closed) | Never truncate the set during rotation |

ytt itself needs no change and has no cache-flush surface on this path — the
bounded staleness is the documented price of the cache (auth.md).

## 6. Subject allowlist changes

Not a rotation in the credential sense — no secret is touched — but it rides
the same machinery (a manifest change and one Recreate swap), so it belongs
in this runbook.

- **Grant:** discover the exact subject with `ytt selftest --show-sub`
  (inside the pod — an operator exec step; the credential-free proxy cannot
  exec), then edit the `YTT_ALLOWED_SUBJECTS` value in
  `declarative-config/k8s/ardenone-cluster/ytt/deployment.yml`, commit,
  push, let ArgoCD sync.
- **Format:** comma-separated entries; an exact email matches
  case-insensitively; an `@domain` entry matches any verified email in that
  exact domain — the leading `@` anchors it, so lookalikes and subdomains do
  **not** match (`tests/unit/test_auth.py`, `TestCheckSubject` /
  `TestSubjectAllowed`).
- **Old/new behavior:** existing sessions survive (their tokens still
  validate — AuthN is untouched); an added subject can call immediately
  after the swap; a removed subject's next tool call is a `403`.
- **Empty = deny all** (fail-closed). Never ship a "temporarily empty"
  allowlist expecting it to be permissive — it denies everything, pinned by
  `TestSubjectAllowlistCoverage` and `test_auth.py::test_deny_empty_allowlist`.
- **Do not "clean up" the legacy `@jedcabanero.com` entry** without checking
  no account still uses it — the history of what happens when a live domain
  is missing is in `deployment.yml`'s allowlist comment (silently zero tools
  on every call, permanently).

## 7. When rotation meets an outage

| Symptom during/after a rotation | What it is | What to do |
|---|---|---|
| ytt pod CrashLoopBackOff after the bounce, logs show a discovery/construction error | The IdP was unreachable **at startup** — discovery is fetched eagerly and construction raises, so the server never binds. This is the *correct* fail-closed posture (`TestDiscoveryOutageFailClosed.test_unreachable_discovery_fails_startup`) | Restore the IdP; the pod recovers on its own restart. **Not** a rollback case |
| CrashLoop with a startup-validation message | A config constraint violated (the message names it — RUNBOOK §5) | Fix the manifest, or §8 |
| Logins 401 between the two pod flips | The expected fail-closed window (§2) | Nothing — it self-heals when the second pod runs the new secret |
| Sessions 401 after ytt's restart only | The rotation working as designed (full logout, §2) | Users re-login; do not "fix" it |
| Everything 401 *and* logins keep failing after both pods are fresh | The two sides genuinely disagree — a bad write (typo, half-written payload) | §8, secret half |
| Health/metrics unavailable entirely | Not an auth problem — the public endpoints stay open through any rotation | Treat as a general outage (RUNBOOK) |

Never "repair" a disagreement by writing values by hand into the k8s
Secrets: that is a forbidden live mutation of ArgoCD-managed resources, and
ESO would revert it at the next refresh anyway. OpenBao → ESO → restart is
the only path the state actually keeps.

## 8. Rollback

Rotation has **two halves, and they roll back independently** — the single
most important line in this runbook:

> Reverting the declarative-config commit does **not** revert the credential.
> OpenBao is not GitOps-managed; the k8s Secrets converge to whatever OpenBao
> holds. "I reverted the PR" is not "I rotated back."

| Symptom | Revert this half | How | Cost |
|---|---|---|---|
| Bad new secret (typo, Authentik never adopted it, logins fail after both pods converged) | The **OpenBao write** | Write the previous value back as a **new** version — same §3.1 pipe recipe with `-cas=<newest version>`. OpenBao's version history is the record; there is no "restore version" step here | Another full logout — every client that re-logged in during the interim logs in again |
| Bad annotation/manifest change (typo in `restartedAt`, wrong file) | The **declarative-config commit** | `git revert` + push (RUNBOOK §5) — the fastest swap there is (image cached) | One ordinary swap gap |
| Both halves are wrong | In the order above: fix the credential first, then the manifest | | Both costs |

Rollback does **not** resurrect: Whisper jobs lost in any swap (RUNBOOK
§2.1), and any client session that was re-established under an interim value
dies again when the value changes again. Orphaned fingerprint directories
accumulate on `/state` — inert; leave them (OAUTH-STATE-RUNBOOK §4.3). Do
not rotate back casually as a "cleanup": every secret flip is a fleet-wide
logout, and OpenBao keeps the full version history of who wrote what, when.

## 9. Secret-safe verification — the rules this runbook assumes

The value is never the evidence; the **property** is.

- **Never** read a secret to stdout, a terminal, a log, a bead, or a commit —
  not even "just to check". A bare `bao kv get` of the OAuth path dumps the
  plaintext into the transcript.
- **Verify the write** by OpenBao metadata: `current_version` incremented,
  history intact (`bao-as openbao-v2-provision bao kv metadata get -format=json …`).
  The provisioning identity can read metadata precisely so this needs no
  read access to the value.
- **Verify delivery** by downstream effect: ExternalSecret `SecretSynced=True`,
  the pod goes `1/1`, `/ytt/health` answers, a real client completes a login
  and a tool call.
- **Values travel by pipe, `@file`, or stdin field — never argv**, and the
  **write-only** identity (`bao-as openbao-v2-provision`) is the default for
  everything in this runbook. Reach for the read identity only when a value
  must actually be *consumed* — which, for rotation, is never.
- **Retained evidence** (beads, tickets) records paths, version numbers,
  timestamps, and exit codes — never values.

## 10. The conformance pins — where this behavior is enforced

Every claim in §2, §5 and §7 is a test; a runbook sentence that stops being
true is a failing suite. Run them against the checkout (they are also the
only sanctioned rehearsal — there is no staging cluster, and rehearsing on
production would mean manufacturing the logout this runbook documents):

| Behavior | Pin |
|---|---|
| Old-secret upstream tokens die; new-secret tokens accepted; verifier holds exactly one static key | `tests/unit/test_oauth_conformance.py` `TestUpstreamSecretRotation` |
| Rotated signing key rejects issued tokens; client-secret rotation rotates the derived signing key too | same file, `TestYTTSignedTokenRotation` |
| Attacker keys, tampered payloads, `alg=none`, malformed bearers → 401, never a raised 500 | same file, `TestInvalidSignatureRejection` |
| JWKS: empty/outage/unknown-kid fail closed; 1 h TTL; rotated-out key dies when the cache expires | same file, `TestJWKSPathFailClosed` |
| Discovery outage fails startup; validation is offline after startup | same file, `TestDiscoveryOutageFailClosed` |
| Allowlist deny-all, 403 coverage on every tool, health+metrics stay open | same file, `TestSubjectAllowlistCoverage`; semantics in `tests/unit/test_auth.py` (`TestCheckSubject`, `TestSubjectAllowed`) |
| Secret rotation orphans the fingerprint directory; same-secret restore requirement; soft-miss on key mismatch | `tests/unit/test_oauth_state_recovery.py` |

```bash
uv run pytest tests/unit/test_oauth_conformance.py -q
uv run pytest tests/unit/test_oauth_state_recovery.py -q
uv run pytest tests/unit/test_auth.py -q
```

The OpenBao half of the procedure (write recipe, identities, CAS discipline)
is DEPLOY-CHECKLIST §"OpenBao: OAuth client credentials" and the instance's
agent-access rules; the one-path-two-consumers wiring is
`ytt-externalsecret.yml` + `authentik-oidc-clients-externalsecret.yml` in
`declarative-config` (mirrored under `deploy/k8s/`, `deploy/README.md`).
