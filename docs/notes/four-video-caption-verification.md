# Four-video caption verification — 2026-10-14

Bead: `ytt-57a6220f`. The user requested a live check of four YouTube videos.
The existing one-pass [caption retest Deployment](../../deploy/k8s/ardenone-cluster/ytt/caption-retest-deployment.yml)
ran `ytt.fetch.fetch_transcript` with `YTT_FETCH_MODE=browser` from
`ardenone-cluster`, the same egress used by the production service. GitOps
commit `cc435064b` selected these IDs. ArgoCD Application
`ytt-ns-ardenone-cluster` was Synced and Healthy, and pod
`ytt-caption-retest-699fb9fc6-j4vng` completed run `20261014-v1` with 4/4
successes. The path column comes from `ytt_browser_fetch_total{outcome="ok"}`
before and after each fetch; every result used the browser, without yt-dlp
fallback. No transcript text or caption URL was logged or persisted.

| Video ID | Path | Source | Language | Segments | Characters | Seconds |
| --- | --- | --- | --- | ---: | ---: | ---: |
| `bYpdCFz7VtA` | browser | automatic captions | en | 382 | 14,051 | 5.26 |
| `UJlS7KTsvWo` | browser | automatic captions | en | 412 | 15,146 | 4.53 |
| `lhGXIGh5oTY` | browser | automatic captions | en | 167 | 6,216 | 4.77 |
| `jgi2-xV81kY` | browser | automatic captions | en | 415 | 15,372 | 8.85 |

This verifies live caption retrieval through ytt's deployed browser fetch
function. The probe bypasses the MCP tool's authentication, rate limiting, and
cache, so it does not establish end-to-end connector behavior.
