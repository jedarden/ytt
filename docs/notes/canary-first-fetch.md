# First positive in-cluster canary fetch — evidence record

Bead `ytt-590de689` (parent `ytt-58325cdf`, plan Proof Obligation: residential
egress). This note is the durable record of the **first caption fetches
attempted and completed from inside ardenone-cluster** — the data point every
earlier passive observation (startup egress probe, `ytt_egress_is_residential`)
was missing.

**Verdict: `outcome=ok` — the Proof Obligation HOLDS.** Three consecutive
canary probes completed a full yt-dlp caption fetch against YouTube from the
cluster, at the exact 600s cadence, with zero failures.

Collected **2026-09-18T23:08–23:21 UTC**, read-only from codinghome:
`kubectl logs` / `kubectl get pod` through the credential-free proxy
(`http://traefik-ardenone-cluster:8001`, RBAC-verified read-only), and instant
Prometheus queries via the cluster's VPN entrypoint
(`https://prometheus-ardenone-cluster-ts.ardenone.com:8444`). A port-forward to
the canary pod's `:8081` was the alternative named in the acceptance criteria,
but `kubectl auth can-i create pods/portforward -n ytt` → `no` for the
read-only identity; the ServiceMonitor scrape (added in
declarative-config `b2e3fa26`) is the sanctioned read path for the canary's
metrics.

## Workload identity

| Field | Value |
|---|---|
| Cluster / namespace | ardenone-cluster / `ytt` |
| Pod | `ytt-canary-7898978459-mw7l4` |
| Node | `k3s-agent-minisforum` |
| Pod IP / phase | `10.42.6.4` / Running |
| Image | `localhost:7439/ronaldraygun/ytt:0.2.20` (fleet pull-through mirror of pinned `ronaldraygun/ytt:0.2.20`) |
| Started at | 2026-09-18T22:53:41Z |
| Command | `ytt canary` (probe loop, `YTT_CANARY_INTERVAL_SEC=600`) |
| Manifests | `declarative-config/k8s/ardenone-cluster/ytt/canary-deployment.yml`, commit `b2e3fa26` (bead `ytt-1fe9215d`) |

## Canary pod logs (raw, via `kubectl logs --timestamps --tail=-1`)

Container logs are stamped `-04:00` (EDT); UTC is +4h. Verbatim:

```
2026-09-18T18:53:57.652554445-04:00 INFO:ytt.canary:Canary metrics server started on :8081
2026-09-18T18:53:57.652723267-04:00 INFO:ytt.canary:Canary probe loop starting (interval=600s, videos=('jNQXAC9IVRw', 'dQw4w9WgXcQ'))
2026-09-18T18:54:00.522506947-04:00 INFO:ytt.canary:Canary probe succeeded for jNQXAC9IVRw
2026-09-18T19:04:02.962802978-04:00 INFO:ytt.canary:Canary probe succeeded for jNQXAC9IVRw
2026-09-18T19:14:05.156001887-04:00 INFO:ytt.canary:Canary probe succeeded for jNQXAC9IVRw
```

In UTC: probe loop start 22:53:57Z; successes at **22:54:00Z, 23:04:02Z,
23:14:05Z** — 602s and 602s apart, matching the 600s loop (drift = probe
duration). Each "succeeded" line means `probe_once_detail` ran
`yt_dlp.extract_info` against `https://www.youtube.com/watch?v=jNQXAC9IVRw`
from this pod and got caption tracks back (`outcome=ok`) — i.e. YouTube did
not block the fetch: the node's egress (AS701 Verizon Business,
`is_residential=true`, per the server pod's startup probe) serves the caption
path.

Only `jNQXAC9IVRw` ever appears: `run_probe_loop` (`ytt/canary.py`)
breaks on the first success, so `dQw4w9WgXcQ` is probed only when
"Me at the zoo" fails. A log showing only the first video is the healthy
shape, not a skipped second target.

## Canary metrics (Prometheus instant query, scrape ts 2026-09-18T23:20:06Z)

Series from the canary pod's own registry (`job="ytt-canary"`,
`instance="10.42.6.4:8081"`, `pod="ytt-canary-7898978459-mw7l4"`):

| Metric | Value | Notes |
|---|---|---|
| `ytt_canary_last_success_timestamp_seconds` | `1789773245.156` = **2026-09-18T23:14:05Z** | Matches the third probe's log line to the second — gauge and logs corroborate each other |
| `ytt_canary_failures_total` | `0.0` | Zero failed probes since pod start |
| `ytt_fetch_blocks_total` | **no series** | See analysis below |
| `ytt_fetch_empty_body_total` | `0.0` | |

Cross-check from the **public server scrape** (`https://mcp.ardenone.com/ytt/metrics`,
server pod `ytt-7956cff687-mswbh`, node `k3s-agent-d`): it still carries
`ytt_egress_is_residential 1.0` and `ytt_canary_last_success_timestamp_seconds 0.0`
— the *server* process registry has never run a probe (expected; separate
process) — and, like Prometheus, has **no `ytt_fetch_blocks_total` series**.

## Why `ytt_fetch_blocks_total{outcome=...}` has no series — and why that no longer means "zero fetch attempts"

Before the canary Deployment existed, the absent series was *interpreted* as
"zero fetch attempts" (parent bead note). That inference is no longer sound:

1. **The counter has no increment site in this release.** `grep -rn
   fetch_blocks` over the package finds only the definition
   (`ytt/observability.py`, `ytt_fetch_blocks_total = Counter(...)`) and unit
   tests. No production code path increments it, so no process — server or
   canary — can emit a series for it in 0.2.20. Its absence is a latent
   instrumentation gap, not evidence about attempts.
2. **The canary's fetch path bypasses it by design.** `probe_once_detail`
   (`ytt/canary.py`) calls yt-dlp directly (same options as `ytt.fetch`, no
   OAuth), and reports per-probe outcome through the `ytt_canary_*` family —
   that is where its `ok` / `ip_blocked` classification lands.

So the positive data point lives in the `ytt_canary_*` metrics and the probe
log lines above, not in `ytt_fetch_blocks_total`. Wiring the counter into
`ytt.fetch` (server tool path) remains worthwhile — the connector's real
traffic still has no outcome series — but is outside this bead's scope
(candidate follow-up; the server-side series is still absent as of this
collection, which *for the server process* still means no tool-driven fetch).

## Conclusion

- **First positive fetches from the cluster: 3 for 3, outcome `ok`.** The
  residential-egress assumption behind the plan's Proof Obligation is now
  backed by completed caption fetches, not just an IP classification.
- The honest falsifier remains live: if YouTube starts blocking the node,
  `ytt_canary_failures_total` rises, the success lines stop, and
  `time() - ytt_canary_last_success_timestamp_seconds > 1800` fires the
  `YttCanaryFailed` alert (`prometheusrule.yml`). At collection time the gauge
  was fresh (~1 min old) and the failure counter zero.
- The Webshare / `YTT_PROXY_URL` fallback (docs/notes/proxy-egress.md) stays
  as designed-in insurance; nothing observed justifies enabling it.
