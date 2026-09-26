# ytt Operator Runbook — oauth-state PVC: backup, restore & state-loss recovery

Everything about the `ytt-oauth-state` volume: what lives on it, why this is
the one ytt volume whose backup is worth the trouble, how to take and restore
a copy, what actually happens when the state is lost anyway, and how to
repair a torn entry without throwing away the whole session population.
The companion volume `ytt-cache` has the opposite value profile — nothing on
it is worth backing up (see [CACHE-RUNBOOK.md](CACHE-RUNBOOK.md) §2).

Related docs:

| Doc | Covers |
|---|---|
| [RUNBOOK.md](RUNBOOK.md) | Upgrade/rollback swaps, state-across-restart, the Recreate model, forbidden kubectl |
| [CACHE-RUNBOOK.md](CACHE-RUNBOOK.md) | The other PVC: value model (nothing worth backing up), ENOSPC, scratch |
| [AUTH-ROTATION-RUNBOOK.md](AUTH-ROTATION-RUNBOOK.md) | The rotations that orphan this state (client secret, signing key), their restart sequencing, rollback, and verification |
| [docs/notes/auth.md](../docs/notes/auth.md) | The OAuth key families — including the client-secret rotation that orphans this state |
| [docs/notes/retention-policy.md](../docs/notes/retention-policy.md) | Why a transcript cleanup must never reach this volume (§7.2) |

Facts marked **verified** were checked live against `ardenone-cluster`, the
installed `fastmcp` 3.4.2 storage code, and the 0.2.21 deployment on
2026-09-25 (bead `ytt-c53a2f56`).

## 1. What lives on the state volume

`ytt-oauth-state` — a **256Mi** `longhorn` PVC (RWO, ns `ytt`), mounted at
`/state` in the single `ytt` pod, with `FASTMCP_HOME=/state` pointing
FastMCP's OAuthProxy at it (`oauth-state-pvc.yml`, `deployment.yml`).
FastMCP persists all OAuth state under `<home>/oauth-proxy/`:

```
/state
└── oauth-proxy
    └── 042c46b2818b                      # key fingerprint, see §1.1
        ├── S_mcp_oauth_proxy_clients-4db71f6a
        │   ├── reg_9xk….json             # one DCR client registration
        │   └── S_mcp_oauth_proxy_clients-4db71f6a-info.json
        ├── S_mcp_upstream_tokens-064b3cac
        │   └── S_subject_example_jedarden_com-….json   # upstream token set
        ├── S_mcp_jti_mappings-…
        ├── S_mcp_refresh_tokens-…
        ├── S_mcp_oauth_transactions-…    # in-flight login only — §1.2
        └── S_mcp_authorization_codes-…   # in-flight login only — §1.2
```

Layout facts (all **verified** against the installed `key_value`/`fastmcp`
storage by writing a store and inspecting the tree):

- **One fingerprint directory per encryption key.** The 12-hex directory name
  is `sha256(storage_encryption_key)[:12]` — a different OAuth client secret
  produces a different directory (§1.1, §4.2).
- Each logical collection is a subdirectory whose name is sanitized from the
  collection name (`mcp-oauth-proxy-clients` →
  `S_mcp_oauth_proxy_clients-4db71f6a`; the `S_` prefix marks a sanitized
  name, and the `-8hex` suffix is a deterministic hash of the *original*
  name, added because sanitization changed it — so on-disk names are stable
  across pods for a given fastmcp version). Each entry is one `<key>.json`
  file; each collection directory is
  paired by an `-info.json` metadata file. Both belong in a backup.
- **Entries are encrypted at rest.** The file is a JSON envelope
  `{"created_at": …, "expires_at": …, "value": {"__encrypted_data__":
  "<base64 Fernet token>", "__encryption_version__": 1}, "version": 1}` —
  only the `value` payload is ciphertext; write a store with known values,
  grep the tree for them → nothing readable leaks (**verified**). Treat the
  tree (and any backup of it) as sensitive regardless: it holds live
  credentials' ciphertext, and the decryption key is derivable from a secret
  in OpenBao (§1.1).
- **The envelope's timestamps are plaintext.** `created_at` is on every
  entry; `expires_at` accompanies it whenever the entry was written with a
  TTL — every collection but client registrations, which are stored without
  one and carry `created_at` alone (**verified** in the envelope JSON and
  pinned by the smoke test). Either field is readable without the key — an
  operator can see how old the youngest and oldest sessions are (and, for
  the token-bearing collections, their expiry edges) with `jq` alone, which
  is the sanctioned way to eyeball the store. Expiry is the envelope's
  `expires_at`, **not** file mtime — unlike the cache volume, tar's mtime
  handling is not load-bearing here (§3).
- There is no lockfile and no dotfile on this volume — nothing to exclude
  from a backup, nothing that must never be restored. (Contrast
  `.ytt-singleton.lock` on the cache volume.)

### 1.1 The key above the volume

The storage encryption key is derived, not stored:

```
YTT_OAUTH_CLIENT_SECRET  (Authentik client secret — OpenBao via ExternalSecret)
  → derive_jwt_key(salt="fastmcp-jwt-signing-key")        # the JWT signing key
  → derive_jwt_key(salt="fastmcp-storage-encryption-key") # the storage key
```

`YTT_JWT_SIGNING_SECRET` (unset in the reference deployment) replaces the
middle step when set. Two consequences an operator must be able to recite:

1. **The volume's root of trust is the OAuth client secret, not the volume.**
   Restored ciphertext is readable only under the secret that encrypted it.
2. **Rotating `YTT_OAUTH_CLIENT_SECRET` (or setting
   `YTT_JWT_SIGNING_SECRET` for the first time) orphans the state**: the new
   key derives a new fingerprint directory, the server starts from an empty
   one, and every client re-authenticates. That is the documented global
   logout lever (auth.md), and it is why a secret rotation and a backup
   restore must never be improvised in the same window.

Decryption failures are **soft by construction**: the proxy builds its store
with `raise_on_decryption_error=False`, so an entry that does not decrypt
reads back as a miss and the client re-registers — a key mismatch can never
500 the server. But note the boundary of that softness: it covers entries
whose *envelope* is intact (§6 for what happens when the file itself is torn).

### 1.2 What is durable and what is not

| Collection (logical name) | Holds | Worth restoring? |
|---|---|---|
| `mcp-oauth-proxy-clients` | One DCR registration per connected client | **Yes** — without it every client must re-register (and re-login) |
| `mcp-upstream-tokens` | The upstream (Authentik) token sets backing each FastMCP session | **Yes** — losing these force-re-logins the client even if its registration survived |
| `mcp-jti-mappings` | FastMCP token ↔ upstream token binding for transparent refresh | **Yes** — without the binding a session can't refresh upstream |
| `mcp-refresh-tokens` | Refresh-token metadata (hashes only) used to validate refresh calls | **Yes** — cheap to carry, painful to drop |
| `mcp-oauth-transactions` | In-flight login state (mid-browser-flow) | Optional — expired in minutes; only a login *in progress at backup time* is affected, and that login just fails once |
| `mcp-authorization-codes` | Outstanding authorization codes | Optional — same lifetime story as transactions |

In practice the backup takes the whole `oauth-proxy` tree — the optional
rows cost kilobytes, and cherry-picking collections adds a failure mode for
no benefit.

## 2. Value model — the one volume worth backing up

**Nothing backs this volume up today.** Verified 2026-09-25: the Longhorn
volume behind the claim (`pvc-dc8b93bd-9749-47b9-bb9d-4cdd573d7b73`, Bound,
1 replica) has **no recurring jobs** (`spec.recurringJobs` empty) and there
are **no backup resources cluster-wide** (`kubectl get backups.longhorn.io
-A` → `No resources found` — same posture the cache runbook recorded for
`ytt-cache`). This is the volume where that stance costs the most: losing it
force-logs-out **every connected client** — an interactive Authentik login
per person and per MCP client — where losing `ytt-cache` costs one
re-fetch per transcript.

What loss does *not* cost is user data: registrations re-create via DCR, the
IdP side is untouched, and the server never hard-fails (§5). The backup's job
is narrowly "avoid the mass re-login", and a backup taken *before* the
incident is the only thing that does that — after loss there is nothing to
restore.

If you take a point-in-time copy, treat it like a credential: the tar holds
the ciphertext of every live access/refresh token, decryptable by anyone who
holds the OAuth client secret. Keep it off shared storage, out of every repo,
and delete it when its retention window ends. Never `cat` an entry file
into a terminal or a log — the plaintext-timestamp fields (§1) are all an
operator ever needs from a file's contents.

## 3. Backup

File-level tar, exactly like the cache runbook's copy — the store is flat
JSON files, no database, no quiescing protocol:

```bash
KC=<a kubeconfig with pods/exec on ns ytt>   # see the boundary below
kubectl --kubeconfig="$KC" exec -n ytt deploy/ytt -c ytt -- \
  tar czf - -C /state . \
  > "ytt-oauth-state-$(date -u +%Y%m%dT%H%M%S).tar.gz"
```

Procedure notes:

- **No exclusions.** Unlike the cache volume there is no lockfile; `-info.json`
  files are part of the store and must come along.
- **Take it whenever.** The server writes entries with atomic
  temp-file-and-rename (`key_value`'s `write_file_atomic`), so even a copy
  taken mid-login cannot observe a half-written entry — worst case the
  snapshot misses the newest in-flight registration, which that client
  renews on its next login.
- **Sanity-check the tar without decrypting anything**: list it, and confirm
  the fingerprint directory is the one the live pod is using (§7 step 1).
- **Access boundary.** The credential-free read-only proxy **cannot exec** —
  `kubectl exec` through it fails with
  `unable to upgrade connection: Forbidden` (re-verified 2026-09-25,
  matching CACHE-RUNBOOK §6.4), and no
  kubeconfig on `codinghome` grants `pods/exec` on `ardenone-cluster`. Backup
  and restore are operator actions; an agent can detect state loss (§5) and
  validate manifests but cannot take the tar itself.

## 4. Restore

### 4.1 Same key first

Before touching the volume, confirm the restore will be *readable*: the
`YTT_OAUTH_CLIENT_SECRET` (and `YTT_JWT_SIGNING_SECRET`, if set) must be the
same value the backup was taken under. It was derived from that secret into
the fingerprint directory name (§1.1), so the check is mechanical: the
backup's `oauth-proxy/<fingerprint>/` name must equal the fingerprint the
live pod derives. You can compare against the last backup of the same
deployment era, or simply confirm no `YTT_OAUTH_CLIENT_SECRET` rotation
happened between backup and restore (auth.md's rotation = global logout).
If the secret *did* change, restoring is pointless-but-harmless (§4.3) — the
real options are "let everyone re-login" or "rotate back" (do not rotate
back casually; OpenBao keeps history for a reason).

### 4.2 The swap procedure

Restore **beside** the live tree and swap directories, rather than untarring
over `/state/oauth-proxy` in place. `tar xzf` writes files directly (no
temp-and-rename), so an in-place untar racing the server's writes is the one
way this store can acquire a torn file (§6) — the two-rename swap makes that
impossible and keeps a rollback tree:

```bash
# 1. unpack beside the live tree (same filesystem, so renames are atomic)
kubectl --kubeconfig="$KC" exec -i -n ytt deploy/ytt -c ytt -- sh -c '
  mkdir -p /state/.restore && tar xzf - -C /state/.restore' \
  < ytt-oauth-state-<ts>.tar.gz

# 2. validate the envelope of every restored entry BEFORE it goes live (§7 step 2)

# 3. swap (single renames; sub-second window where reads miss)
kubectl --kubeconfig="$KC" exec -n ytt deploy/ytt -c ytt -- sh -c '
  mv /state/oauth-proxy /state/oauth-proxy.pre-restore &&
  mv /state/.restore/oauth-proxy /state/oauth-proxy'

# 4. bounce the pod through the GitOps door (RUNBOOK.md §1/§7): any manifest
#    change triggers the Recreate swap; `kubectl delete pod` is forbidden.
```

- The bounce (step 4) is not required for *reads* — the store is
  read-through per request — but it is the step that makes the restore
  deterministic: it re-derives the fingerprint directory from the current
  secret, drops anything cached in memory, and leaves the pod in the same
  state a fresh bind of a pre-populated volume would be in. Do not skip it.
- **File mtimes are irrelevant** (§1): expiry lives in the envelope's
  `expires_at`, which the tar preserves because it preserves file *contents*.
- The 256Mi volume holds both trees comfortably — the store is registrations
  and token sets, kilobytes per client — but `df -h /state` first is free.
- Leftovers: delete `/state/.restore` and `/state/oauth-proxy.pre-restore`
  once §7 passes (both are operator-verified inert, not auto-cleaned).

### 4.3 If the key mismatched anyway

A restore under a different secret leaves the live pod pointing at its *own*
fingerprint directory (the backup's directory name is different), so the
server keeps missing and clients re-register — the pre-fix behavior, no
worse than doing nothing. The restored tree sits on the volume as an orphan
directory. Do not delete orphaned fingerprint directories "for hygiene"
while the pod is running; they are inert, and volume bytes are not the
constraint at 256Mi.

## 5. State loss — what actually happens

Losing the volume (accidental PVC deletion, Longhorn failure, a restore that
had to be abandoned) is an **availability inconvenience for users, not an
incident for the server**. The failure mode, in order:

1. The server keeps booting and serving. The proxy creates an empty
   fingerprint directory on first use; every storage read misses; nothing
   raises. This is the same code path a first-ever boot takes, pinned by
   `tests/unit/test_oauth_state_recovery.py` (wiped-volume scenario).
2. Every connected client's next call fails authentication (its FastMCP
   token's registration/JTI binding no longer resolve) and the client
   re-runs the full flow: discovery → DCR (a *new* registration is written)
   → interactive Authentik login → new tokens. Users see "sign in again".
3. Logins that were mid-browser-flow at the moment of loss fail at the
   callback (their transaction row is gone) and the user restarts the login.
4. There is nothing to clean up anywhere else: Authentik's client is
   unaffected, the transcript cache is unaffected, in-flight Whisper jobs
   were already lost to the pod swap that any volume operation here entails.
5. If the loss was a PVC *deletion*, replacement goes through the GitOps
   door — `kubectl delete pvc` is on RUNBOOK's forbidden list for both PVCs,
   and the claim re-creates from `oauth-state-pvc.yml` empty.

Recovery from loss is therefore "announce the re-login" (or restore §4, if a
backup predating the loss exists — after loss there is nothing to restore).

## 6. Torn entries — the failure mode a careless restore creates

The store's *writes* are atomic and its *decryption misses* are soft (§1.1) —
but a **structurally broken entry file** (truncated, non-JSON, wrong
envelope shape) is neither: the store raises a per-key
`DeserializationError` while reading it, and the proxy's `get_client` does
not catch storage errors. Practically:

- One torn file makes **that one key's** reads raise — e.g. a torn
  registration file turns the affected client's authorize/token handling into
  server errors — while every sibling entry keeps working.
- The soft-miss magic does not apply, because the failure happens in the
  file/envelope layer *before* decryption is ever attempted.

This is pinned by `tests/unit/test_oauth_state_recovery.py` (torn-entry
scenario), and it is why §4.2 validates every envelope before the swap and
prefers the atomic directory swap over an in-place untar. If a torn file is
found on a live volume (or a restore must proceed anyway):

- **Targeted repair** — delete the one torn `<key>.json` inside its
  collection directory. The next read of that key is a clean miss: for a
  registration, the affected client re-registers (re-login); for an upstream
  token set, that session re-logins. Everything else stays up.
- **Re-register over it** — a fresh write to the same key replaces the torn
  file atomically (the server's own DCR path does this after the repair
  above; the smoke test pins that overwriting a torn key heals it).
- **Never** delete the whole volume or the whole collection because one file
  is torn — that converts a one-client repair into the §5 fleet-wide event.

## 7. Post-restore validation (run in order)

Steps 1–3 read only file *shape* — no entry is decrypted, no secret material
is printed:

1. **Fingerprint agrees.** The live tree has exactly one fingerprint
   directory and it matches the restored/backup one:
   `kubectl --kubeconfig="$KC" exec -n ytt deploy/ytt -c ytt -- sh -c
   'ls /state/oauth-proxy'` — a second directory here means a key rotation
   happened (§4.3); figure out which one the pod is using
   (`ls -t`, or check for recent writes) before proceeding.
2. **Every envelope is intact** — the §6 guard. Inside the pod
   (`python3`/`jq` whichever the image carries; this is the check the smoke
   test pins): every `.json` under `/state/oauth-proxy` parses, entry files
   carry both `__encrypted_data__` and `__encryption_version__`, and no
   entry file is zero bytes. `find /state/oauth-proxy -name '*.json'
   -size 0` must come back empty.
3. **Collections look populated** — per-directory file counts roughly match
   the backup's (`tar tzf ytt-oauth-state-<ts>.tar.gz | grep -c json` vs the
   live count); `mcp-oauth-proxy-clients` should have one entry per
   connected client. The plaintext `expires_at` fields (§1) show live
   sessions' expiry edges without decrypting anything.
4. **The bounce happened and the pod is one, ready** — `kubectl get pods -n
   ytt` (through the credential-free proxy) shows a single fresh `1/1`
   `ytt` pod.
5. **Functional: an existing client does NOT re-login.** The whole point of
   the restore: the next call from a previously-connected MCP client must
   succeed silently. If clients are bouncing to the login page, §4.3 (key
   mismatch) is the first suspect — check which fingerprint directory the
   fresh pod created.
6. **Record it** — backup filename, fingerprint dir, per-collection counts,
   swap timestamp, on the bead or ticket for the operation. A restore nobody
   recorded is indistinguishable from a mystery.

## 8. Recovery drill — the runnable smoke

The behavioral contract this runbook leans on is pinned as a runnable smoke
in `tests/unit/test_oauth_state_recovery.py`:

```bash
uv run pytest tests/unit/test_oauth_state_recovery.py -q
```

It exercises, at the unit level, against the real storage stack the proxy
builds (same key-derivation chain, same file-tree store, same encryption
wrapper): a wiped volume degrading to clean misses and rebuilding via
re-registration; a backup → swap-restore → round-trip preserving
registrations and token sets byte-for-field; the same-secret requirement
(a rotated secret changes the fingerprint directory, reads go soft-miss, the
old tree orphans harmlessly); a torn entry file raising per-key while
siblings serve, healing by overwrite or targeted delete; corrupt ciphertext
in an intact envelope degrading to a silent miss; the backup's opacity (no
plaintext secret material anywhere in the tree) and the §7.2 envelope check
classifying every file; and the runbook ↔ manifest drift guard.

As with the cache drill, these tests are the only sanctioned practice path —
there is no staging cluster, and rehearsing on production would mean
manufacturing the §5 event the runbook exists to avoid.
