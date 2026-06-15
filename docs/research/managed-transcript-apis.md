# Managed / Hosted YouTube Transcript APIs

**Research date:** 2026-06-14
**Context:** `ytt` is a remote MCP server (Python) that fetches YouTube transcripts. The hard 2026 problem
is YouTube blocking datacenter IPs and PoToken bot detection. This doc evaluates *not* self-hosting the
fetcher and instead paying a managed API that absorbs the residential-proxy / PoToken arms race.

---

## Summary

- The IP-blocking + PoToken problem is real and current. YouTube blocks datacenter/cloud IPs (AWS, GCP,
  etc.), and the free/datacenter proxy tiers are *also* blocked. The only thing that reliably works is
  rotating **residential** proxies, plus keeping up with PoToken/InnerTube changes. That is exactly the
  maintenance burden a managed API removes.
- For a **personal, low-volume** use case (a few to a few-tens of transcripts/day), a managed API is the
  pragmatic default. The two best-fit dedicated vendors are **TranscriptAPI.com** (~$5/mo, 1,000 credits,
  100 free credits) and **Supadata** ($5/mo Basic / $17/mo Pro, 100 free credits/mo). Both have free tiers
  that may *fully cover* genuinely low personal volume (100 calls/mo ≈ 3/day).
- **Supadata is the better single pick if you need ASR** (it transcribes videos with no captions via
  Whisper through the same endpoint). **TranscriptAPI is cheaper per credit and has richer YouTube
  search/channel/playlist endpoints**, but does *not* document an ASR fallback — no-caption videos return 404.
- The **official YouTube Data API v3 `captions.download` is NOT an option** for arbitrary third-party
  videos: it requires OAuth edit permission on the video (you must own it / be authorized). It cannot fetch
  transcripts for videos you don't control.
- Reliability numbers from vendors ("99.97% uptime", "49 ms median", "15M transcripts/month") are
  **marketing, not independently verified**. Treat them as directional, not guaranteed. None publish an SLA
  for the cheap tiers.
- **Bottom line:** For personal low-volume use, go managed. Start on a free tier (TranscriptAPI or Supadata),
  upgrade to the ~$5 tier only if you exceed it. Self-hosting yt-dlp + Webshare residential proxies is
  viable but costs money *and* ongoing arms-race maintenance, which is the wrong trade at hobby volume.

---

## 1. Supadata (`supadata.ai`)

**API shape.** Single REST endpoint: `GET https://api.supadata.ai/v1/transcript?url=<youtube-url>` with an
`x-api-key` header. Also has Python and JS SDKs, plus channel/playlist endpoints. Returns JSON with a `lang`
field and a `content` array of segments.

**What it returns.** Text **with millisecond-precision timestamps** — each segment has `text`, `offset` (ms),
and `duration` (ms). Optional `lang` parameter to request a specific language.

**ASR / no-caption handling.** **Yes — this is its differentiator.** When a video has no captions (live
streams, old uploads, UGC), Supadata generates the transcript with **Whisper AI** through the same endpoint.
Vendor states "high accuracy, typically above 90% word accuracy."

**Languages.** "50+ languages, auto-detected," with optional per-request language selection and a paid
translation operation.

**Pricing (vendor-stated, monthly; credits do NOT roll over):**

| Plan | Credits/mo | Price/mo | Rate limit |
|---|---|---|---|
| Free | 100 | $0 | 1 req/sec |
| Basic | 300 | $5 | 10 req/sec |
| Pro | 3,000 | $17 | 10 req/sec |
| Mega | 30,000 | $47 | 50 req/sec |
| Giga | 300,000 | $297 | 100 req/sec |
| Supa | 1,000,000 | $897 | 100 req/sec |
| Enterprise | custom | custom | custom |

**Credit cost per operation:** standard caption retrieval = **1 credit**; **generated (ASR) transcript = 2
credits per minute of video**; translation = 30 credits/min. So ASR is meaningfully more expensive than
pulling existing captions — a 20-minute no-caption video burns ~40 credits.

**Rate limits.** Tiered by plan (1–100 req/sec as above). Free tier is 1 req/sec.

**Reliability claims.** Page says "Reliable" as a design principle but **publishes no uptime %, SLA, or
median latency.** Independently unverified.

**IP/proxy handling.** The product premise is that Supadata absorbs the blocking problem server-side; it does
not expose proxy config to the caller. No public detail on *how* (residential proxy farm assumed). This is
the whole point — you don't run proxies.

## 2. TranscriptAPI.com (`transcriptapi.com`)

**API shape.** Base URL `https://transcriptapi.com/api/v2`. Endpoints:
`GET /youtube/transcript`, `GET /youtube/search`, `GET /youtube/channel/resolve` (free),
`GET /youtube/channel/search`, `GET /youtube/channel/videos`, `GET /youtube/channel/latest` (free, RSS),
`GET /youtube/playlist/videos`. Richer YouTube-data surface than Supadata.

**What it returns.** Configurable via `format` (`json`|`text`) and `include_timestamp` (bool). JSON segments
have `text`, `start`, `duration`; text mode emits lines like `[123.45s] text`. Includes a `language` field.
So: **text + timestamps, yes.**

**ASR / no-caption handling.** **Not documented / apparently no.** No mention of ASR fallback; videos without
captions return **HTTP 404**. This is the key gap vs Supadata. (Marketing emphasizes "bypassing YouTube
blocks," not speech-to-text.)

**Languages.** Returns a `language` field but doesn't enumerate supported languages; appears tied to
YouTube's existing auto-generated/manual captions rather than its own ASR.

**Pricing (vendor-stated):**

| Plan | Credits/mo | Price | Rate limit | Top-ups |
|---|---|---|---|---|
| Free | 100 (one-time trial) | $0, no card | — | — |
| Monthly | 1,000 | $5/mo | 200 RPM | $2.50 / 1,000 |
| Annual | 1,000/mo | $54/yr ($4.50/mo) | 300 RPM | $1.50 / 1,000 |

**Credit model.** 1 credit per successful request (HTTP 200) or cached response; channel-resolve and
latest-uploads-RSS are free. **Failed (4xx/5xx) and rate-limited (429) requests do NOT consume credits** —
a genuinely nice property: you only pay for transcripts you actually get.

**Rate limits.** 200 RPM (monthly) / 300 RPM (annual), per API key. Returns `X-RateLimit-*` headers; 429 on
breach. Recommends exponential backoff on 408/429/503.

**Reliability claims (vendor-stated, unverified):** "500K+ transcripts processed daily," "15M+ transcripts
served last month," "49 ms median response time." No published SLA. Treat as marketing.

**Verdict vs Supadata.** Cheaper per credit ($5/1,000 vs Supadata's $5/300), pay-only-on-success, and better
search/channel/playlist tooling — but **no ASR**, so it can't serve no-caption videos. Pick TranscriptAPI if
you only ever need videos that already have captions; pick Supadata if you need the ASR fallback.

## 3. `youtube-transcript-api` (jdepoix) + Webshare; and RapidAPI listings

**Does the popular OSS library have its own hosted offering?** **No.** `jdepoix/youtube-transcript-api` is a
free, open-source Python library — there is no first-party hosted/SaaS endpoint. What it *does* offer is a
built-in **Webshare residential-proxy integration** (since v1.0.0) via `WebshareProxyConfig`, because the
maintainer tested providers and found Webshare **Residential** proxies the most reliable. Important caveat
from the docs: you must buy the **"Residential"** package — **NOT** "Proxy Server" (datacenter) or "Static
Residential," which don't work reliably against YouTube. So this is still a **self-hosting** path (you run
the library + pay Webshare), not a managed API. It's the baseline the managed options are competing against.

GitHub issues (e.g. #511, #593, #504) confirm the lived reality in 2026: direct calls work locally, get
blocked on cloud IPs; free/datacenter proxies are blocked; residential works but costs money and adds
external dependency; and users explicitly ask whether managed APIs (Supadata) have become the standard. This
corroborates that the problem is real and that managed APIs are a legitimate answer.

**RapidAPI marketplace.** Many small YouTube-transcript APIs are listed (e.g. by `mahmudulhasandev`,
`thisisgazzar`, `8v2FWW4H6AmKw89`, Solid API, etc.), most advertising ~100 free requests/month. These are
generally thin resellers wrapping the same scraping/proxy stack with no published SLA or reliability track
record, and variable maintenance. They're worth knowing as fallbacks but are **not** a safer default than the
two dedicated vendors above; treat any reliability claim as unverified and expect churn.

## 4. Official YouTube Data API v3 `captions.download` — NOT an option (and why)

This is the only *official* Google path, and it **cannot fetch transcripts for arbitrary third-party videos.**

- `captions.download` "**requires the user to have permission to edit the video**." If you lack edit
  permission you get **HTTP 403**: "The permissions associated with the request are not sufficient to
  download the caption track."
- It requires OAuth 2.0 with scope `https://www.googleapis.com/auth/youtube.force-ssl` (or
  `youtube.force-ssl`/`youtubepartner`) — i.e. an authenticated user who **owns or is authorized on** the
  channel.
- Net effect: you can only download captions for **videos your own authenticated account controls.** There
  is no API-scope way to be granted third-party access; the owner would have to add you as a manager in
  their content/CMS. For a service that ingests *arbitrary* user-supplied YouTube URLs, this is a
  **non-starter.**

**Conclusion:** The official API is fine for "transcribe my own uploads" and useless for "transcribe any
public video a user pastes." `ytt`'s use case is the latter, so the official API is excluded.

## 5. General-purpose ASR as a managed fallback

For no-caption videos, a generic ASR/transcription API (Whisper-as-a-service, Deepgram, AssemblyAI, etc.)
could transcribe the audio — **but those APIs need the AUDIO as input.** Getting the audio means downloading
the stream from YouTube, which **reintroduces the exact datacenter-IP/PoToken download problem** you were
trying to outsource. So a generic ASR API is only useful **if it itself ingests a YouTube URL and does the
fetching for you.**

The clean way to get ASR *without* re-owning the download problem is therefore a transcript API that bundles
ASR server-side — which is precisely what **Supadata's Whisper fallback** does (2 credits/min). For `ytt`,
prefer Supadata's built-in ASR over bolting on a separate Whisper API; the latter only makes sense if you
already solved audio download (i.e. you're self-hosting anyway).

## 6. Comparison & cost for personal low-volume use

Scenario: **a few to a few-tens of transcripts/day** (~30–600/month).

| Option | Returns text+timestamps | ASR (no-caption)? | Free tier | Entry paid price | Reliability (claimed) | Independently verified? |
|---|---|---|---|---|---|---|
| **Supadata** | Yes (ms timestamps) | **Yes (Whisper, 2 cr/min)** | 100 cr/mo | $5/mo (300 cr) → $17/mo (3,000 cr) | "reliable", no SLA | No |
| **TranscriptAPI.com** | Yes (configurable) | No (404 on no-caption) | 100 cr (trial) | $5/mo (1,000 cr) | "49ms / 15M-per-mo", no SLA | No |
| **youtube-transcript.io** | Yes (not fully documented) | Not documented | 25/mo | $9.99/mo (1,000) | none | No |
| **RapidAPI listings** | Varies | Usually no | ~100/mo typical | varies | none | No |
| **yt-dlp + Webshare residential (self-host)** | Yes | Only if you add ASR + audio dl | 10 free static IPs (datacenter, blocked) | ~$3.50/GB residential (promo) + your compute/maintenance | n/a | self-measured |
| **YouTube Data API captions.download** | n/a | n/a | free quota | free | n/a | **owned videos only — excluded** |

**Cost read for personal use:**
- At **≤100 transcripts/month** (~3/day, captions-only), **both Supadata Free and TranscriptAPI's free
  credits can cover you at $0** — start there.
- At **tens/day** (e.g. 600/mo): **TranscriptAPI $5/mo (1,000 cr)** is the cheapest headroom for
  caption-only needs; **Supadata Pro $17/mo (3,000 cr)** if you also need ASR (remember ASR is 2 cr/min, so
  a handful of long no-caption videos eats credits fast).
- **Pragmatic default if going managed:** **TranscriptAPI free → $5/mo** for caption-only; **Supadata
  free → $17/mo Pro** if ASR for no-caption videos matters. TranscriptAPI's "pay only on success" (no charge
  on 4xx/5xx/429) is a real plus for a hobby budget.

## 7. Tradeoffs vs self-hosting yt-dlp + residential proxy

**Self-host (yt-dlp / youtube-transcript-api + Webshare residential):**
- *Pros:* no per-call lock-in; URLs never leave your infra (privacy); marginal cost is just bandwidth
  (~$3.50/GB promo residential, transcript fetches are tiny KB-scale so bandwidth is cheap); full control.
- *Cons:* **you own the arms race** — PoToken/InnerTube changes, library upgrades, proxy rotation, breakage
  triage. Residential proxies cost money *and* you still babysit the code. Datacenter/free proxies don't
  work; "Residential" (not "Static Residential") is the only reliable Webshare tier. This is recurring
  unpaid maintenance, which is the real cost at hobby scale.

**Managed API:**
- *Pros:* they absorb IP-blocking + PoToken; near-zero maintenance; free tiers may fully cover personal
  volume; ASR available (Supadata) without you ever touching audio download.
- *Cons:* **vendor lock-in / dependency** (if they break or get blocked, you're stuck); **privacy** — you
  send every video URL (and thus the user's interests) to a third party; credits can run out; reliability is
  vendor-stated and unverified; no SLA on cheap tiers.

**Cost crossover.** Self-hosting's monetary cost is dominated by the residential proxy subscription
(Webshare residential starts around a few $/mo minimum) plus your time. Managed APIs are ~$0–5/mo at personal
volume. **Below a few-thousand transcripts/month, managed is both cheaper and lower-effort** — the crossover
where self-hosting wins on pure $ is at much higher, sustained volume where per-credit fees exceed a flat
proxy bill, *and* where you have the engineering time to maintain the fetcher. That crossover is far above
personal use.

**Privacy note for `ytt`:** because it's a remote MCP server handling *user-supplied* URLs, sending those URLs
to Supadata/TranscriptAPI leaks what users are watching to a third party. If that matters, self-hosting is the
only way to keep URLs in-house — a non-cost reason one might still self-host despite the maintenance burden.

---

## Recommendation

**For `ytt` at personal / low-volume use, go managed.** Default to a dedicated transcript API:
- **TranscriptAPI.com** if you mostly need videos that already have captions (cheapest, pay-on-success,
  good channel/search tooling) — free trial then $5/mo.
- **Supadata** if you need transcripts for videos with **no captions** (its Whisper ASR fallback is the
  cleanest way to do that without re-owning audio download) — free 100/mo then $17/mo Pro.

Keep `yt-dlp + Webshare residential` as a documented **self-host fallback** for (a) privacy-sensitive
deployments where URLs must not leave your infra, or (b) volume high enough that flat proxy cost beats
per-credit fees. Do **not** rely on the official YouTube Data API for third-party videos — it's owner-only.

Validate any vendor before committing real load: their uptime/latency numbers are marketing, not verified.

---

## Sources

- Supadata transcript API docs/landing — https://supadata.ai/youtube-transcript-api
- Supadata pricing — https://supadata.ai/pricing
- Supadata "best YouTube transcript API" blog — https://supadata.ai/blog/best-youtube-transcript-api
- TranscriptAPI.com landing/pricing — https://transcriptapi.com/
- TranscriptAPI.com API reference — https://transcriptapi.com/docs/api/
- TranscriptAPI vs Supadata (vendor blog, biased) — https://transcriptapi.com/blog/transcriptapi-vs-supadata
- youtube-transcript.io pricing — https://www.youtube-transcript.io/pricing
- jdepoix/youtube-transcript-api README (Webshare residential integration) — https://github.com/jdepoix/youtube-transcript-api/blob/master/README.md
- Issue #593 — cloud IP blocking 2026, Webshare vs Supadata — https://github.com/jdepoix/youtube-transcript-api/issues/593
- Issue #511 — "YouTube is blocking requests from your IP" even with Webshare — https://github.com/jdepoix/youtube-transcript-api/issues/511
- YouTube Data API v3 captions implementation guide — https://developers.google.com/youtube/v3/guides/implementation/captions
- YouTube Data API v3 captions.download reference (403 / edit-permission requirement) — https://developers.google.com/youtube/v3/docs/captions/download
- Webshare residential proxy pricing (~$3.50/GB promo) — https://hostadvice.com/proxy-servers/webshare-review/
- RapidAPI YouTube transcript API listings (overview) — https://rapidapi.com/mahmudulhasandev/api/youtube-transcript-api1
- API.market roundup of YouTube transcript APIs 2026 — https://api.market/blog/magicapi/youtube-transcript/top-youtube-transcript-apis
