# ytt Canary Monitoring Runbook — continuous freshness & failure alerts

How a **persistent** canary failure or a **stale** last-success timestamp is
detected *after* deployment, and what to do when it is.  This is the
standing-monitoring counterpart of the two one-shot checks that already
exist:

| Check | When | Where |
|---|---|---|
| `ytt canary --once` | Ad-hoc egress proof from wherever the command runs | [RUNBOOK.md](RUNBOOK.md) §3, `docs/notes/canary-first-fetch.md` |
| `ytt canary --gate` | Post-deploy release gate (image or egress change) | [RUNBOOK.md](RUNBOOK.md) §3.1 decision table |
| **This runbook** | **Continuously, from the `ytt-canary` Deployment** | alert rules in [`prometheusrule.yml`](k8s/ardenone-cluster/ytt/prometheusrule.yml) |

The gate proves the *new* pod can reach YouTube at release time; it says
nothing about the next hour.  The canary Deployment re-proves it every
`YTT_CANARY_INTERVAL_SEC` (600 s in production), and the alerts below turn a
persistent failure into a page instead of something noticed next release.

Related docs: [RUNBOOK.md](RUNBOOK.md) (upgrade/rollback door, §5; gate
decision table, §3.1), [CACHE-RUNBOOK.md](CACHE-RUNBOOK.md),
[docs/notes/canary-first-fetch.md](../docs/notes/canary-first-fetch.md)
(what the `ytt_canary_*` signals looked like on first collection),
[docs/notes/proxy-egress.md](../docs/notes/proxy-egress.md) (the
`YTT_PROXY_URL` fallback design).

## 1. Signal catalog

All series come from the **canary pod's own `/metrics` on :8081**
(prometheus_client registry), scraped by the `ytt-canary` ServiceMonitor
endpoint.  They are emitted by `ytt/canary.py::run_probe_loop`, which every
cycle walks the fixed video ladder **directly** and — when `YTT_PROXY_URL`
is configured — **through the proxy** (the same two paths the gate probes
once).

| Series | Meaning |
|---|---|
| `ytt_canary_last_success_timestamp_seconds` | Unix time of the last cycle in which **any** path succeeded.  This is what `YttCanaryFailed` watches: stale means *neither* path has worked for the window — transcript fetches are down. |
| `ytt_canary_failures_total` | Cycles in which **no** path succeeded. |
| `ytt_canary_probe_last_success_timestamp_seconds{probe}` | Per-path freshness: last cycle in which **that path** succeeded.  `probe="direct"` (native egress) or `probe="via_proxy"` (through `YTT_PROXY_URL`). |
| `ytt_canary_probes_total{probe, outcome}` | One increment per path per cycle, labelled by how the ladder **terminated**: `outcome="ok"` or a stable `ytt.errors` error code (`ip_blocked`, `empty_body`, `rate_limited`, `unavailable`, …). |

Three properties of these series that matter when reading them:

- **Freshness gauges initialize to loop-start time, not 0.**  A pod restart
  cannot fire a staleness alert before the first probe completes, and
  `time() - gauge` can never exceed process age — a gauge older than the pod
  is not observable.  Right after a restart every gauge is fresh *by
  construction*; give one full interval (10 min) before reading meaning
  into them.
- **The `via_proxy` series exists only while `YTT_PROXY_URL` is
  configured.**  Production runs proxy-unset by design, so its absence on
  the scrape means "this canary does not probe via proxy", **never** "the
  proxy is broken".  An alert keyed on the proxy path must not fire on an
  absent series — and none of the rules in §2 do (an absent operand makes a
  comparison return the empty vector, which cannot fire).
- **Counter children are pre-registered as zero** for every canonical
  outcome at loop start (same convention as
  `ytt.observability.FETCH_BLOCK_OUTCOMES`), so an absent
  `ytt_canary_probes_total` child means "process predates this change",
  never "metric not registered".

The **server** pod also exports the overall pair — registered at import,
never updated (the server process runs no probe loop).  Only the
`job="ytt-canary"` series are meaningful; the server's copies sit at their
boot values.  Series from an image **older** than this change carry only
the overall pair (see §6).

## 2. Alert catalog

Canonical definitions: `deploy/k8s/ardenone-cluster/ytt/prometheusrule.yml`
(the applied mirror in `jedarden/declarative-config`, synced by ArgoCD —
`tests/unit/test_deploy_parity.py` keeps the two byte-identical).
`tests/unit/test_canary_monitoring.py` drift-guards every binding below
against both this runbook and `ytt/canary.py` — edit them together or the
suite fails.

| Alert | Severity | Fires when | Users |
|---|---|---|---|
| `YttCanaryFailed` | critical | No path has succeeded for > 1800 s | **Down** — incident |
| `YttCanaryDirectBlocked` | warning | `direct` stale > 1800 s while `via_proxy` fresh | Served (proxy fallback) |
| `YttCanaryFallbackBroken` | warning | `via_proxy` stale > 1800 s while `direct` fresh | Served (direct) |
| `YttCanaryProbeFlapping` | warning | > 50 % of one path's probes failing over 30 m | Depends on timing |

What "fresh" and "stale" mean numerically, and why 1800 s: the loop runs
every 600 s, so a path whose gauge is older than 1800 s has missed **three
consecutive cycles**; the *fresh* side of the paired alerts uses one
interval (< 600 s).  The overall `YttCanaryFailed` expression is kept
byte-identical to the pre-monitoring rule — old and new images satisfy it
the same way.

### 2.1 `YttCanaryFailed` — fetches down (incident)

```promql
time() - ytt_canary_last_success_timestamp_seconds > 1800
```

Neither path has completed a caption fetch for 30 minutes.  The canary
videos are old, stable, always-captioned; three consecutive misses on
*both* paths is not a YouTube blip.  Transcript fetches are down for users
— this is the "treat as an incident" row of the gate table (RUNBOOK §3.1),
fired by standing monitoring instead of a release gate.  Severity is
**critical** for that reason.

### 2.2 `YttCanaryDirectBlocked` — degraded, proxy carrying users

```promql
time() - ytt_canary_probe_last_success_timestamp_seconds{probe="direct"} > 1800
  and ignoring(probe)
  time() - ytt_canary_probe_last_success_timestamp_seconds{probe="via_proxy"} < 600
```

Native egress is blocked (most likely the residential IP burned) while the
proxy fallback is demonstrably working.  Users are still served — the fetch
path retries `ip_blocked` through `YTT_PROXY_URL` — at residential-proxy
bandwidth cost.  The `and ignoring(probe)` is load-bearing: without it the
two sides carry different `probe` labels, match nothing, and the alert can
never fire.

### 2.3 `YttCanaryFallbackBroken` — fallback unavailable, direct healthy

```promql
time() - ytt_canary_probe_last_success_timestamp_seconds{probe="via_proxy"} > 1800
  and ignoring(probe)
  time() - ytt_canary_probe_last_success_timestamp_seconds{probe="direct"} < 600
```

The mirror of §2.2: direct is fine, the proxy path is not.  No user impact
*yet*; the exposure is having no fallback the moment the direct IP burns.
Cannot fire when no proxy is configured or on an image without per-path
series — the left operand is empty and an empty vector never fires.

### 2.4 `YttCanaryProbeFlapping` — intermittent failures staleness misses

```promql
sum by (probe) (rate(ytt_canary_probes_total{outcome!="ok"}[30m]))
  / sum by (probe) (rate(ytt_canary_probes_total[30m])) > 0.5
```

A path failing every *other* cycle keeps its freshness gauge refreshing, so
both staleness alerts stay quiet while the path is half-dead.  This rule
fires when more than half of a path's ladder terminations in a 30-minute
window were non-`ok`.  Which outcome dominates says why — break
`ytt_canary_probes_total` down by `outcome` (§3) before acting.

## 3. Triage commands

Steps 1–3 are read-only, all through the credential-free proxy.  When an
alert fires, establish **which path, which outcome, since when** before
touching anything:

```bash
KS="kubectl --server=http://traefik-ardenone-cluster:8001"

# 1. What the loop itself has been saying (per-path lines carry probe=…):
$KS logs -n ytt deploy/ytt-canary --timestamps | tail -20

# 2. Freshness of each path, right now — Prometheus instant query via the
#    cluster's VPN entrypoint (same path as docs/notes/canary-first-fetch.md):
curl -sk "https://prometheus-ardenone-cluster-ts.ardenone.com:8444/api/v1/query" \
  --data-urlencode 'query=time() - ytt_canary_probe_last_success_timestamp_seconds' \
  | jq -r '.data.result[] | "\(.metric.probe) stale by \(.value[1]) s"'

# 3. Why a path is failing — terminations by outcome:
curl -sk "https://prometheus-ardenone-cluster-ts.ardenone.com:8444/api/v1/query" \
  --data-urlencode 'query=sum by (probe, outcome) (increase(ytt_canary_probes_total[1h]))' \
  | jq -r '.data.result[] | "\(.metric.probe)/\(.metric.outcome): +\(.value[1])"'

# 4. Ground truth, one probe each way, from the canary pod's own environment.
#    An operator step: exec needs `create` on `pods/exec`, which the
#    credential-free proxy's RBAC withholds (RUNBOOK §7).  Through $KS,
#    steps 1–3 are the ground truth.
KC=<a kubeconfig with pods/exec on ns ytt>
kubectl --kubeconfig="$KC" exec -n ytt deploy/ytt-canary -c ytt-canary -- ytt canary --once
kubectl --kubeconfig="$KC" exec -n ytt deploy/ytt-canary -c ytt-canary -- ytt canary --once --via-proxy   # only if YTT_PROXY_URL is set
```

In-pod ground truth needs a kubeconfig granting `pods/exec` on ns `ytt` —
the credential-free proxy cannot exec (`unable to upgrade connection:
Forbidden`; RUNBOOK §7), so the one-shot probes are an operator action and
steps 1–3 are what an agent runs.  The one-shot canary never touches the
singleton lock.  The `--once` reports are JSON with credential-redacted
error strings — retain them as evidence for the bead or incident record,
exactly like gate evidence.

## 4. Response — rollback vs. escalation

The rollback door is always the same one: **`git revert` the
declarative-config commit and push** (RUNBOOK §5).  Never
`kubectl rollout undo` (§7).  The decision is keyed by alert and triage,
mirroring the gate table's vocabulary (RUNBOOK §3.1):

| Firing alert + triage | Response |
|---|---|
| `YttCanaryFailed`, outcome `ip_blocked`, **and a release in the last day changed fetch code or bumped yt-dlp** | **Roll back** the tag pin (RUNBOOK §5), confirm the alert clears within one interval, re-run the gate on the previous tag. An extractor regression is the classic cause. |
| `YttCanaryFailed`, outcome `ip_blocked`, no relevant release | **Escalate — incident.** Both egress paths are blocked while users need fetches: page the egress owner with the `--once` evidence from both probes. A rollback cannot fix burned egress. If no proxy is configured, this is also the moment to consider enabling `YTT_PROXY_URL` (docs/notes/proxy-egress.md). |
| `YttCanaryFailed`, outcome `empty_body` / `rate_limited` / `unavailable` | Not an egress verdict. `empty_body` on known-good videos → suspect an extractor regression: roll back if a release shipped, else escalate to the maintainers (the fixed video list itself may need updating — canary videos age too). |
| `YttCanaryDirectBlocked` (proxy fresh, direct stale) | **Escalate to the egress owner** — the native IP is blocked. Users are served via the fallback; a rollback will not help and is not required. Decide explicitly whether any in-flight release should wait (fallback bandwidth is metered). Track `ytt_fetch_blocks_total`-class signal on the server for user-path confirmation. |
| `YttCanaryFallbackBroken` (direct fresh, proxy stale) | **Escalate to the proxy/egress owner** — the proxy's IP is burned or its quota exhausted. No user impact yet; fix the fallback before the direct path also burns. If a release just changed `YTT_PROXY_URL` or proxy handling, **roll back** that manifest change instead. |
| `YttCanaryProbeFlapping` | Triage §3 step 3 first. Outcome `ip_blocked` flapping on `direct` → prelude to §2.2, same response. Flapping on both paths or `empty_body`-shaped → suspect extractor rot; escalate to the maintainers with the outcome breakdown. |
| Any alert immediately after a canary pod restart | Wait one full interval (600 s) before acting — boot-initialized gauges are fresh by construction (§1); a real persistent condition re-fires. A flapping alert that does not survive two evaluations was noise. |

Two standing rules, inherited from the gate: **re-check before acting**
(step 4 of §3 gives ground truth in seconds), and **retain the evidence**
— probe JSON, alert screenshot, triage output — on the bead or incident
record.  An alert resolved without evidence is indistinguishable from an
alert that resolved itself.

## 5. Verification — the drift guards

Three artifacts must agree, and the suite enforces it
(`tests/unit/test_canary_monitoring.py`):

1. **Code** — `ytt/canary.py` defines the metric names, the
   `direct`/`via_proxy` vocabulary (pinned equal to
   `ytt.canary_gate.PROBE_ORDER`) and the outcome labels.
2. **Rules** — `deploy/k8s/ardenone-cluster/ytt/prometheusrule.yml` (and
   its applied twin in declarative-config) define the four alerts of §2,
   referencing exactly those metric names, label values and the canonical
   `YttCanaryFailed` expression.
3. **This runbook** — documents every alert with its severity and the
   canonical expressions verbatim.

After any change to one of the three:

```bash
uv run pytest tests/unit/test_canary_monitoring.py -q     # the three-way guard
scripts/definition-of-done.sh                             # includes mirror parity
```

and for the applied cluster state, confirm the rule Prometheus actually
loaded matches the manifest (ArgoCD sync first if it lags):

```bash
$KS get prometheusrule ytt -n ytt -o jsonpath='{.spec.groups[0].rules[*].alert}'
```

## 6. Rollout and image compatibility

The per-path pair and `YttCanaryProbeFlapping` need an image that carries
this monitoring code.  Until such an image is pinned (anything ≤ 0.2.20),
the canary exports only the overall pair: `YttCanaryFailed` — whose
expression is byte-unchanged — keeps watching, and the three per-path
alerts simply stay silent (their operand series are absent, and an absent
series cannot fire an alert).  No manifest order matters: rules may land
before or after the image; neither order false-fires.

When the monitoring image is pinned, nothing here needs re-doing — the
`via_proxy` series appears only if `YTT_PROXY_URL` is configured on the
canary Deployment (it is deliberately unset in production; adding it is an
operator decision, and until then the proxy-path alerts remain dormant by
design, not by omission).
