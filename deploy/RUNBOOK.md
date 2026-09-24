# ytt Operator Runbook — single-replica upgrade & rollback

How to take `ytt` from *new code merged* to *validated in production* on
`ardenone-cluster`, and how to get back when it goes wrong.  The deployment is
a **single replica with `strategy: Recreate`** — every swap is a brief,
deliberate outage, and this runbook explains what happens in that window, what
state survives it, and what must never be done directly with `kubectl`.

Related docs:

| Doc | Covers |
|---|---|
| [DEPLOY-CHECKLIST.md](DEPLOY-CHECKLIST.md) | Release SOP (VERSION bump → CI image → pin tag) + human-gated steps |
| [README.md](README.md) | The `deploy/` ↔ `declarative-config` mirror and how to refresh it |
| [docs/notes/single-replica.md](../docs/notes/single-replica.md) | Why `replicas: 1` is a correctness constraint, not a sizing choice |
| [docs/usage/deploy-ardenone.md](../docs/usage/deploy-ardenone.md) | Architecture, routing, secrets, observability |
| [docs/notes/canary-first-fetch.md](../docs/notes/canary-first-fetch.md) | Canary evidence and what the `ytt_canary_*` signals mean |

## Source of truth and the one legitimate write path

The applied manifests live in **`jedarden/declarative-config`**
(`k8s/ardenone-cluster/ytt/`), synced by ArgoCD Application
`ytt-ns-ardenone-cluster` with `selfHeal` on.  The only sanctioned change is:

```
edit declarative-config → commit → push → ArgoCD syncs
```

If a sync lags, force it from the ArgoCD UI — that *applies the repo state*,
it does not bypass GitOps.  Everything `kubectl` can do to these objects is
either read-only or forbidden (§7).

## 1. Upgrade path (GitOps)

### 1.1 Build the image

Covered by [DEPLOY-CHECKLIST.md](DEPLOY-CHECKLIST.md) §1–§3, condensed:

1. Bump all six version-bearing files — `VERSION`, `pyproject.toml`,
   `uv.lock` (regen with `uv lock`), `ytt/__init__.py`, `README.md`
   (quick-start image), `docs/usage/self-hosting.md` (compose image) — plus
   a `CHANGELOG.md` section and compare links, in one commit in
   `jedarden/ytt`; tag it `v<version>` (annotated) and push the commit and
   the tag to Forgejo.  `scripts/definition-of-done.sh` fails the gate when
   any of these drift apart (added after 0.2.20 shipped with five of the six
   still at 0.2.19 and no tag — bead `ytt-d18f0ab1`).
2. The push webhook fires `ytt-sensor` → `ytt-build` (iad-ci): the `VERSION`
   bump is validated, the Dockerfile's test stage runs
   `pytest -m "not integration"` as a build gate, and `ronaldraygun/ytt:<version>`
   is pushed to Docker Hub.
3. Watch the workflow (Argo UI at `https://argo-ci.ardenone.com`, or the
   `kubectl get workflows` recipe in DEPLOY-CHECKLIST §2).  **Do not pin a tag
   that has not finished building** — the pin step is what makes the cluster
   pull it.

### 1.2 Swap the deployment

The image tag is pinned by hand (CI never auto-bumps the deployment):

```
declarative-config/k8s/ardenone-cluster/ytt/deployment.yml
- image: ronaldraygun/ytt:<old>
+ image: ronaldraygun/ytt:<new>
```

Commit + push.  ArgoCD syncs, and because the strategy is `Recreate` the
cluster performs the kill-then-start swap described in §2.

Anything that changes a manifest goes through this same door — env var
changes, allowlist entries, resource changes — not just image tags.  One env
var caveat: `YTT_CACHE_MAX_BYTES` must stay comfortably **below** the PVC
request (2Gi); startup validates the cap against `statvfs` on the mounted
filesystem, which reports less than the nominal request (ext4 reserved
blocks), and an over-large cap CrashLoopBackOffs the pod on boot.  The
deployment pins 1800Mi against the 2Gi `ytt-cache` PVC for exactly this
reason.

### 1.3 What the cluster does during the swap

Timeline for an image-tag bump (`Recreate`, no surge pod ever exists):

| Phase | What happens | Typical duration |
|---|---|---|
| Terminate | Old pod gets SIGTERM; uvicorn stops accepting and waits for in-flight requests, capped by the 30 s default grace period, then SIGKILL.  The singleton `flock` dies with the process (kernel-held; no cleanup, ever). | ≤ 30 s |
| Gap | **No ytt pod exists.**  This is the outage window's floor. | seconds |
| Schedule + pull | New pod scheduled; `imagePullPolicy: IfNotPresent` pulls the new tag through `docker-hub-registry`.  First pull of a fresh tag is the dominant, most variable cost. | tens of seconds — minutes |
| Boot | Observed live (pod `ytt-7956cff687-mswbh`, 2026-09-18): storage validation → `singleton_lock_acquired` → `Server startup` → egress probe (~0.14 s) → uvicorn listening < 0.2 s after the lock. | < 1 s of app boot |
| Ready | Readiness probe (`/ytt/health`, initialDelay 5 s, period 10 s) passes; endpoints flip over.  First 200 observed ~7 s after process start. | ~5–15 s |

Two properties worth internalizing:

- **The flock makes an overlap impossible, not just unlikely.**  If the
  strategy were ever `RollingUpdate` (default `maxSurge ≥ 1`), the new pod
  would boot *while the old one still holds the lock*, log
  `Single-replica invariant violated` with the recorded holder
  (pid/hostname/started_at/version), exit 1, and CrashLoopBackOff until the
  old pod exited — downtime by crash-loop instead of by design.
  `Recreate` is load-bearing; `tests/unit/test_single_replica.py` asserts it
  (and `replicas: 1`) on every Deployment under `deploy/k8s/` so an edit
  can't silently reintroduce rolling swaps.
- **A crash-looping pod never wedges the lock.**  The kernel releases the
  `flock` on every process death, so a bad image that dies at startup is
  always recoverable by rollback (§5) without touching the lockfile.  Never
  delete `/cache/.ytt-singleton.lock` — the file is a holder *record* for
  error messages; the lock is the `flock` itself, and the dotfile is
  invisible to the cache scan by design.

### 1.4 Client impact during the swap

Single replica = no HA, accepted for v1 (plan §"Single replica = no HA").
During the window, MCP calls fail to connect; the Claude connector retries.
A client mid-pagination can resume once the server is back (the cursor is
client-supplied; completed transcripts are re-served from cache — see the
caveat in §2.2).

## 2. State across an upgrade

| State | Where | Survives the swap? |
|---|---|---|
| Transcript cache files (`*.txt` + `.json` sidecars) | `ytt-cache` PVC (2Gi longhorn, `/cache`) | **Files yes** — the PVC is untouched by any upgrade or rollback |
| Cache in-memory inventory + LRU byte-counter | process | No — see the wiring gap below |
| OAuth client registrations + issued tokens (FastMCP OAuthProxy) | `ytt-oauth-state` PVC (256Mi, `/state` via `FASTMCP_HOME`) | **Yes** — connected clients do *not* re-login on upgrade.  This is the PVC's entire reason to exist |
| Whisper job registry (in-flight + completed ASR jobs) | process | **No** — every job is lost (§2.1) |
| Whisper scratch audio (partial downloads) | `emptyDir` (600Mi cap) | No — dies with the pod, by design |
| Rate-limit buckets, Whisper quotas, single-flight map | process | No — budgets reset to full; a retry mid-window re-fetches |

### 2.1 In-flight Whisper jobs are lost — say so to users

The ASR job registry is in-memory.  A pod swap destroys it:

- A transcription running at SIGTERM is killed (grace period caps it at 30 s,
  far below a long video's `YTT_WHISPER_TIMEOUT_SEC`), and its scratch audio
  vanishes with the pod.
- A client that afterwards polls `get_transcript_job` gets `not_found`.
  The tool's contract handles this: re-call `get_youtube_transcript`, which
  re-kicks the job from scratch (fresh audio download, fresh quota spend).

Operational consequence: **schedule upgrades for a quiet period**, and if an
ASR-heavy session is in flight (`YTT_MAX_PENDING_WHISPER_JOBS` queue deep,
`ytt_whisper_*` metrics active), let it drain before pinning the new tag.

### 2.2 Known gap: the cache is *preserved* but not *re-registered* after restart (as of 0.2.20)

Honest operator expectation-setting: the plan calls for `startup_scan()` to
rebuild the cache inventory from the PVC on boot, but as of 0.2.20 that wiring
is not on the boot path (no `cache_startup_scan` event in the observed startup
logs, and no call site in the package).  Practical effect of an upgrade:

- Transcripts cached *before* the swap sit on the PVC but are **not served**
  until the same `(video_id, lang)` is fetched again — the post-upgrade cache
  behaves cold.
- The LRU byte-counter only ever sees post-restart units, so pre-restart files
  are invisible to eviction and slowly accumulate toward the PVC cap across
  restarts (bounded by the 2Gi volume; harmless for a long while, not free).

This is a code fix, not an operational workaround — see bead
`ytt-4f1c45c2` in the ytt workspace.  Until it lands, "cache preserved" in the
table above means *files preserved*, not *warm*.

## 3. Post-deploy validation (run in order)

All `kubectl` here is read-only through the credential-free proxy — allowed.

```bash
KS="kubectl --server=http://traefik-ardenone-cluster:8001"
```

1. **Pod ready, one replica, right tag**
   ```bash
   $KS get deploy -n ytt                          # ytt AND ytt-canary, both 1/1
   $KS get pods -n ytt -o wide                    # new pod Running, age seconds
   $KS get deploy ytt -n ytt -o jsonpath='{.spec.template.spec.containers[0].image}'
   ```
2. **Health** — `curl -s https://mcp.ardenone.com/ytt/health` → `{"status": "ok"}`.
3. **OAuth metadata + ibkr do-no-harm gate** — DEPLOY-CHECKLIST §6 verbatim:
   ytt's `/.well-known/*` must show `resource`/`issuer` `…/ytt`, and ibkr's
   two metadata hashes must be byte-identical to pre-deploy.  Any ibkr hash
   change → revert the declarative-config commit and push.
4. **Canary acceptance gate, in the new server pod** — the release gate after
   any image or egress change:
   ```bash
   $KS exec -n ytt deploy/ytt -- ytt canary --gate \
     | tee "canary-gate-$(date -u +%Y%m%dT%H%M%SZ).json"
   ```
   The gate runs `ytt canary --once` (direct) **and** `--via-proxy` when
   `YTT_PROXY_URL` is configured, requires `outcome=ok` on every probe,
   writes the combined JSON evidence (default
   `/tmp/ytt-canary-evidence/`), and exits 0 **only** on a full pass.  The
   `tee` copy is the retained evidence for the release record (the pod's
   `/tmp` dies with the pod) — paste it into the release bead.  Exit 1 = gate
   failed: the report's `remediation` field names the rollback/escalation
   path (§3.1 below) and the report's `probes` half carries the per-probe
   detail.  Like `ytt canary --once`, the gate does not touch the singleton
   lock — only `serve()` does — so it is safe alongside the live server; the
   same is true of `ytt selftest`.  (A stray `ytt serve` exec'd into the pod
   *will* exit 1 on the lock — that's the tripwire working.)
5. **Standing canary Deployment is healthy** — `ytt-canary` is a separate
   Deployment and does **not** restart when the server does; check its probe
   loop kept succeeding through the upgrade:
   ```bash
   $KS logs -n ytt deploy/ytt-canary --timestamps | tail -5   # "Canary probe succeeded"
   ```
   Metrics via the `ytt-canary` ServiceMonitor scrape (`:8081`):
   `ytt_canary_failures_total` steady at its pre-upgrade value and
   `time() - ytt_canary_last_success_timestamp_seconds` < 600.  The
   `YttCanaryFailed` alert fires only past 1800 s, so *no* alert ≠ *validated*;
   run step 4 regardless.
6. **Metrics + egress classification** — `curl -s
   https://mcp.ardenone.com/ytt/metrics | grep -E 'ytt_egress_is_residential'`
   must read `1.0`.  `YttEgressNotResidential` firing after an upgrade means
   the new pod is egressing from somewhere unexpected — check the startup
   egress log line on the new pod.
7. **Optional, heavier** — in-cluster integration suite from a pod with a
   checkout: `ytt test --integration` (see `docs/usage/deploy-ardenone.md`).
   Needs residential egress; never chases these from a datacenter machine.

### 3.1 The canary acceptance gate — decision table

Step 4's gate (`ytt canary --gate`, bead `ytt-026fdbb4`) encodes this
section mechanically; the table below is the same logic in prose (mirrored by
`ytt.canary_gate.remediation_for`, the two are updated together).  It applies
to **any** release that touched the image or the egress configuration — a
new pinned tag, a `YTT_PROXY_URL` change, a proxy-provider swap.

**Rule: the release is not done until the gate exits 0.**  Green health
probe + unchanged OAuth metadata + no firing alerts do *not* prove the new
pod can reach YouTube; only the gate does.

On a failure: **re-run the gate once before acting** — single failures can
be transient (a YouTube-side blip, a proxy flap).  On a repeat failure, act
on the **first failing probe** (`report.failed_probe`) and its
`outcome` (`report.verdict`):

| First failing probe | `outcome` | Action |
|---|---|---|
| `via_proxy` (proxy configured) | `ip_blocked` | The fallback path is broken. If the release changed `YTT_PROXY_URL`/proxy handling → **roll back** (revert the declarative-config change, push; §5) and re-gate. Otherwise the proxy's residential IP is burned or quota exhausted → **escalate** to the proxy/egress owner with the evidence JSON; rollback will not help. Direct probe failing too = fetches down for users → treat as an incident. |
| `direct` (proxy configured, `via_proxy` passed) | `ip_blocked` | Native egress blocked, proxy healthy — the caption path degrades to its proxied fallback (bandwidth cost, still serving). Not a release defect; **rollback will not fix it**. **Escalate** to the egress owner with the evidence, and decide explicitly whether to keep or revert the tag. |
| `direct` (no proxy configured) | `ip_blocked` | If the release changed fetch code or bumped yt-dlp → **roll back** (§5) and re-gate on the previous tag. Otherwise the egress IP is burned → **escalate** to the egress owner with the evidence. |
| either probe | anything else (`empty_body`, `private`, `rate_limited`, …) | Not an egress verdict — on the known-good canary video this is most likely a yt-dlp/extractor regression shipped in the new image → **roll back** (§5) and re-gate. If the release changed no fetch code → **escalate** to the maintainers with the evidence (the fixed canary video list itself may need updating). |
| either probe | `gate_error` | The gate crashed before a verdict — a tooling failure, **not** a canary result. Fix the environment and re-run; escalate with the evidence only if it persists. |

**Retain the evidence either way.**  The `tee`d stdout copy (or
`report.evidence_file` where the filesystem survives) goes into the release
bead — pass or fail.  A pass without retained evidence is an unauditable
release; a failure without evidence is an escalation nobody can act on.
The report contains no secrets: probe error strings are credential-redacted
and the proxy URL never appears.

## 4. Refresh the in-repo mirror

`deploy/k8s/` mirrors the applied manifests and is enforced byte-for-byte by
`tests/unit/test_deploy_parity.py` wherever a `declarative-config` checkout
sits alongside (part of `scripts/definition-of-done.sh`).  After every
declarative-config change, refresh the mirror with the canonical commands in
[README.md](README.md) — in the same release commit, or the next one.

## 5. Rollback

Rollback is the same door as rollout, in reverse — a git revert, not a kubectl
command:

```bash
cd <declarative-config checkout>
git log --oneline -3 -- k8s/ardenone-cluster/ytt/deployment.yml   # find the tag-bump commit
git revert <commit>
git push
# ArgoCD syncs; Recreate swaps the pod back to the previous pinned tag.
```

- **Speed:** the previous tag is usually already in the node's image cache
  (`IfNotPresent`), so the rollback swap skips the pull phase of §1.3 and is
  typically the fastest swap you can do.  If the pod lands on a different
  node, expect one pull.
- **What rollback restores:** the previous server behavior.  Nothing else —
  and nothing is destroyed by it.  Both PVCs stay untouched (revert a
  Deployment image/env change only), so cache files and OAuth state carry
  across the round trip.
- **What it does not resurrect:** Whisper jobs lost in the forward direction
  stay lost (§2.1) — clients re-kick.  Rate-limit and quota budgets reset
  again on the rollback swap.
- **Bad image that crash-loops at boot:** every crash releases the flock
  (§1.3), so just revert promptly; each minute in crash-loop is the same
  outage as any other swap gap, repeated.  There is no lock cleanup step and
  none is ever needed.
- **Bad config change (env var, wrong tag typo):** same revert path.  A
  startup-validating mistake (e.g. `YTT_CACHE_MAX_BYTES` over the usable
  volume size) shows up as CrashLoopBackOff with the validation error in
  `kubectl logs` — read it before reverting; it names the exact violated
  constraint.

## 6. Alerts that can fire around an upgrade

| Alert | Fires during an upgrade? | Note |
|---|---|---|
| `YttCanaryFailed` | No | The canary is a separate Deployment that keeps probing through the server swap |
| `YttEgressNotResidential` | Only if the new pod genuinely egresses wrong | Startup probe result; investigate, don't wait it out |
| `YttWhisperDown` | No (rate window 10 min) | A single mid-swap Whisper failure won't trip it |
| `YttHomeIPBurned` | No | Cannot fire at all as of 0.2.20: `ytt_fetch_blocks_total` has no increment site in the release (see `docs/notes/canary-first-fetch.md`; pre-registration tracked as bead `ytt-f77d1be4`) |
| `YttCacheUndersized` | Only after the §2.2 wiring gap is fixed | Evictions post-restart are from a cold-counter baseline until then |

## 7. What must NOT be done directly with kubectl

`selfHeal` reverts live mutations — so a direct kubectl change doesn't stick,
fights the controller while it lasts, and leaves no manifest record.  These
are forbidden, not just discouraged:

| Forbidden | Why specifically |
|---|---|
| `apply` / `create` / `patch` / `edit` / `annotate` / `label` / `replace` / `delete` on the Deployment, Service, IngressRoute, etc. | Reverted by `selfHeal`; the sanctioned path is a declarative-config commit ("Source of truth", above) |
| `kubectl scale` (any direction) | Up is a correctness violation, not capacity: at `replicas ≥ 2` the second pod fails the flock and CrashLoopBackOffs *by design*, and every in-process structure (cache counter, single-flight, job registry, rate buckets) is per-process.  Down is a pointless outage.  Capacity tuning = env vars in the manifest (§1.2) |
| `kubectl rollout restart` | A live mutation with no manifest record.  To bounce the pod, make the manifest say so (any applied change triggers the swap) and let ArgoCD sync |
| `kubectl rollout undo` | Reverted by `selfHeal` the moment it syncs — a rollback that isn't one.  Rollback = `git revert` in declarative-config (§5) |
| `kubectl delete pod` | An unsanctioned restart: same downtime and same in-flight-job loss as a real swap (§2), with no git history explaining it.  Pod deletions are not "cleanup" — the ReplicaSet wants that pod |
| `kubectl delete pvc` (`ytt-cache`, `ytt-oauth-state`) | Destroys the transcript cache **and** every connected client's OAuth session (forced re-login everywhere).  There is no undo |

Always allowed (read-only): `get`, `describe`, `logs`, and `exec` for
diagnostics and the canary (`ytt canary --once`, `ytt selftest`) — through
the credential-free proxy
(`kubectl --server=http://traefik-ardenone-cluster:8001 …`) or any read-only
kubeconfig.  The proxy's RBAC cannot write, so a denied write there is the
boundary working, not an outage to route around.
