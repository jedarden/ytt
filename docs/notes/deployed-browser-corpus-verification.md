# Deployed-browser 429-corpus verification — 2026-10-24

Bead `ytt-8f748cf7`. The 36-video corpus that failed upstream caption-track
HTTP 429 through yt-dlp (run `20260928-v1`, bead `ytt-cd9e04a5`: 31 of 36
`error:rate_limited`) was re-run through the **deployed browser-primary fetch**
after the `ytt` → `ytt-browser:3001` NetworkPolicy egress/ingress fix (bead
`ytt-1e4c448b`) went live — the condition whose absence made prod fall back to
yt-dlp on every fresh fetch.

Run `20261024-v1` of the [caption retest Deployment](../../deploy/k8s/ardenone-cluster/ytt/caption-retest-deployment.yml):
image `ronaldraygun/ytt:0.2.28` (the production image), `YTT_FETCH_MODE=browser`,
`YTT_BROWSER_WS_URL=ws://ytt-browser.ytt.svc.cluster.local:3001/ytt`, from
`ardenone-cluster` — the same egress as the production `ytt` pod. GitOps commit
`4b4faf2f1` (merge `fd50613f5`), ArgoCD Application `ytt-ns-ardenone-cluster`
Synced, pod `ytt-caption-retest-5d6ccff65f-x25hq` (node `k3s-agent-minisforum`),
browser server `ytt-browser-6b44ff69bd-p7kkj` (Running, 0 restarts). Paced 8 s
apart, 41 fetches, no transcript text or caption URL logged or persisted.

The `path` column comes from `ytt_browser_fetch_total{outcome="ok"}` before and
after each fetch; non-zero segment/character counts are the stealth-posture
evidence — the 2026-10-07 diagnostics (bead `ytt-204947b5` / `ytt-daefe30b`)
showed PO-token-rejected automation receives HTTP 200 with a zero-byte body, so
a zero count would mean the token gate is NOT cleared. Every `ok` row below
carried a real timedtext body.

## Known-429 corpus (36 videos)

| Video ID | Path | Source | Language | Segments | Characters | Seconds |
| --- | --- | --- | --- | ---: | ---: | ---: |
| `1DaMkTuiCEQ` | browser | caption_auto | en | 346 | 12,351 | 3.53 |
| `3lUyMZpAb6g` | browser | caption_auto | en | 290 | 8,882 | 3.17 |
| `75tja_RxKn4` | browser | caption_auto | en | 262 | 9,747 | 3.28 |
| `82u2qUjnJ6E` | browser | caption_auto | en | 1,091 | 38,113 | 3.16 |
| `86ptx1cYv5c` | browser | caption_auto | en | 512 | 15,850 | 3.49 |
| `8DgRqDukf-U` | browser | caption_auto | en | 509 | 17,925 | 3.2 |
| `8zHUvixZAtg` | browser | caption_auto | en | 498 | 14,050 | 3.2 |
| `9KOau7CLCCU` | browser | caption_auto | en | 193 | 6,535 | 3.4 |
| `9kacICxX8AY` | browser | caption_auto | en | 445 | 16,382 | 3.6 |
| `BA_fC95p43c` | browser | caption_auto | en | 585 | 21,164 | 3.4 |
| `DlQyXhjH2jE` | browser | caption_auto | en | 494 | 18,388 | 2.65 |
| `GFtFIxrAjIs` | browser | caption_auto | en | 460 | 17,053 | 3.09 |
| `GlW4N7p5KS8` | browser | caption_auto | en | 1,347 | 49,804 | 3.33 |
| `IG3ZYezz7b0` | browser | caption_auto | en | 312 | 11,517 | 2.83 |
| `MnwCQZ_6Ssg` | browser | caption_auto | en | 1,348 | 39,333 | 2.96 |
| `Oa3MUGsFays` | browser | caption_auto | en | 521 | 18,311 | 3.86 |
| `SCBJVRFX_D8` | browser | caption_auto | en | 157 | 5,647 | 3.47 |
| `T1H_TWuaYao` | browser | caption_auto | en | 699 | 19,827 | 3.11 |
| `TwT8oJpZYM0` | browser | caption_auto | en | 967 | 29,214 | 3.28 |
| `UGDIdJh0w_0` | browser | caption_auto | en | 416 | 15,245 | 3.49 |
| `XG6pZQvum8Q` | browser | caption_auto | en | 1,068 | 34,162 | 3.48 |
| `YcuCM3IF_VU` | browser | caption_auto | en | 576 | 21,395 | 2.74 |
| `YrO9TArSK6o` | browser | caption_manual | en-US | 226 | 18,546 | 3.2 |
| `ZH6zuWDcEGg` | browser | caption_auto | en | 1,995 | 70,960 | 3.14 |
| `a9YpxcbBI9g` | browser | caption_auto | hi | 184 | 6,702 | 3.16 |
| `cmnXNkby2SE` | browser | caption_auto | en | 2,492 | 90,393 | 3.75 |
| `d2W4C2lBfaM` | browser | caption_auto | en | 108 | 3,572 | 3.56 |
| `f25UQrHhZo8` | browser | caption_auto | en | 2,174 | 69,897 | 3.51 |
| `h-mDRcIRD5Y` | browser | caption_auto | en | 204 | 6,967 | 3.54 |
| `iWmTuDAHl7Q` | browser | caption_auto | en | 434 | 15,863 | 3.28 |
| `iqEFMb1aKNU` | browser | caption_auto | en | 1,272 | 46,695 | 3.33 |
| `izoYwxgMv8s` | browser | caption_auto | en | 696 | 25,904 | 3.31 |
| `t-zltcuUasM` | browser | caption_auto | en | 1,624 | 57,663 | 3.14 |
| `wfn9DU6Yxdc` | browser | caption_auto | hi | 195 | 7,232 | 3.32 |
| `xYULUSKqQIk` | browser | caption_auto | en | 624 | 23,034 | 3.61 |
| `zlit6xVbf_o` | browser | caption_auto | en | 818 | 30,383 | 3.45 |

**36/36 ok, all 36 via the browser — zero residuals in the corpus** (vs 5/36 ok
through yt-dlp in run `20260928-v1`, where the other 31 got HTTP 429). Fetch
seconds min/median/max: 2.65/3.32/3.86.

## Special cases (5)

| Video ID | Path | Source | Language | Segments | Characters | Seconds |
| --- | --- | --- | --- | ---: | ---: | ---: |
| `iG9CE55wbtY` | browser | caption_manual | en | 427 | 17,148 | 2.95 |
| `dQw4w9WgXcQ` | browser | caption_manual | en | 61 | 2,029 | 2.74 |
| `jNQXAC9IVRw` | browser | caption_manual | en | 6 | 212 | 3.16 |
| `jfKfPfyJRdk` | error | `is_livestream` | — | — | — | 2.25 |
| `aaaaaaaaaaa` | error | `unavailable` | — | — | — | 1.68 |

Expected shapes held: `iG9CE55wbtY` multi-language, `dQw4w9WgXcQ` manual
captions, `jNQXAC9IVRw` the canary video all ok; `jfKfPfyJRdk` correctly
classified `is_livestream` and `aaaaaaaaaaa` `unavailable` (browser
`video_error` outcomes, not infrastructure failures) — 39/41 ok overall.

## Scope

This verifies the deployed fetch path end to end from the production cluster
egress. The probe bypasses the MCP tool's authentication, rate limiting, and
cache (same shape as the [four-video note](four-video-caption-verification.md)),
so it does not by itself establish connector behavior; prod-side confirmation
of a real browser-path fetch through the NetworkPolicy fix is tracked on bead
`ytt-1e4c448b`.

