# MCP tool contract

The complete reference for ytt's two MCP tools: exact arguments and defaults,
response shapes per status, pagination and cursor-staleness behavior, Whisper
job polling states, and the stable error-code taxonomy. The `tools/list`
descriptions are deliberately compact; this document is the contract they
abbreviate.

This contract is executable: [`tests/unit/test_mcp_tool_contract.py`](../../tests/unit/test_mcp_tool_contract.py)
drives the real Streamable HTTP transport (initialize handshake → `tools/list`
→ `tools/call`) and pins everything stated here. Operational HTTP routes
(`/health`, `/metrics`, `/admin/egress`, the RFC 9728 metadata documents) are
covered separately in [`docs/notes/http-endpoints.md`](../notes/http-endpoints.md);
server configuration is in [`configuration.md`](configuration.md).

## Wire basics

- **Transport:** MCP Streamable HTTP at the server URL
  (`YTT_PATH_PREFIX`, default `/ytt/` → endpoint `…/ytt`). OAuth 2.1 bearer
  auth is required on every request.
- **Unauthenticated** requests get HTTP `401` with a
  `WWW-Authenticate: Bearer … resource_metadata="…"` challenge pointing at the
  RFC 9728 protected-resource metadata.
- **Authenticated but not allowlisted** (`YTT_ALLOWED_SUBJECTS`) sessions see
  an **empty** `tools/list` and `isError: true` results whose text reads
  `Authorization failed…` — a protocol-level denial with **no**
  `structuredContent`. The transcript pipeline runs not at all for such a
  caller. There is no `forbidden` tool payload: the allowlist is enforced
  before tool dispatch.
- **Tool payloads** are the JSON dicts documented below, delivered as the MCP
  result's `structuredContent` (mirrored as a JSON text block for clients that
  read only `content`).

## Response envelope

Every tool returns a `TranscriptResult` dict. Only two fields are always
present; everything else is status-dependent:

| Field | Type | Present when |
|---|---|---|
| `video_id` | str | always — the canonical 11-char id |
| `status` | str | always — `ok` \| `partial` \| `pending` \| `running` \| `error` |
| `error_code`, `message` | str | `status="error"` |
| `eta_sec`, `message` | float or `null`, str | `pending` / `running` — `eta_sec` is `null` when the video duration is unknown |
| `text`, `is_final`, `offset`, `total_chars` | str, bool, int, int | `ok` / `partial` |
| `next_cursor` | str | `partial` only (never on a final page) |
| `source`, `lang`, `transcript_quality` | str | transcript delivered (`ok` / `partial`) |
| `segments` | list | transcript delivered, when segment data exists |
| `title`, `channel`, `duration_sec`, `published` | — | when known from video metadata |
| `requested_lang`, `available_langs`, `message` | — | language-fallback advisories (see below) |

`message` is always written to be relayed **verbatim** to the end user — it
carries the ETA, the retry hint, or the failure explanation.

## Tool 1: `get_youtube_transcript`

Fetch the transcript of a YouTube video. Signature:

```
get_youtube_transcript(url, lang?, mode?, cursor?, start?, end?, query?)
```

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `url` | str | *required* | Any YouTube URL form or bare video id (below). |
| `lang` | str \| null | `null` | Requested caption language, BCP-47 tag (`"en"`, `"es"`). `null` = the video's original/default language (English preferred). |
| `mode` | str | `"full"` | `"full"`: return everything inline when it fits (else page). `"chunk"`: always paginate, regardless of length. Any value other than `"full"` paginates. |
| `cursor` | str \| null | `null` | Continuation cursor from a previous `next_cursor`. Omit on the first call. |
| `start` | float \| null | `null` | Window start in **seconds**. |
| `end` | float \| null | `null` | Window end in **seconds**. |
| `query` | str \| null | `null` | Case-insensitive substring filter. Mutually exclusive with `start`/`end` — if both are sent, `query` wins and the time bounds are ignored. |

### URL forms

`watch?v=…`, `youtu.be/…`, `/shorts/…`, `/live/…`, `/embed/…`, `/v/…`, the
`m.`/`music.` subdomains, `youtube-nocookie.com`, and the bare 11-character
video id all canonicalize to the same video and therefore the same cache unit.
Playlist, channel, handle, and search URLs are rejected with
`error_code="bad_url"`.

### Language selection

Priority with `lang` set: manual track for `lang` → auto track for `lang` →
manual default → auto default → any. With `lang` omitted: original/default →
English → any. The response reports what was actually served:

- `lang` — the served language tag (or `"whisper"` for ASR output);
- `requested_lang` — set only when it differs from the served language;
- `available_langs` — the caption tracks the video offers;
- `message` — the fallback advisory, e.g. `requested 'es' unavailable; served 'en'`.

A cache unit is keyed `(video_id, served_lang)`: pass the **served** `lang` on
follow-up calls (pagination, filters) to hit the cache instead of re-fetching.
A caption lookup also falls back to a cached `whisper` unit, so an ASR result
satisfies any `lang`.

### Time and query filters

- `start`/`end` select every segment whose `[start, start+duration]` window
  **overlaps** the requested range — a segment straddling the boundary is
  included. Either or both bounds may be given.
- `query` is a case-insensitive substring match over segment text; matches are
  returned with **±2 segments of context** on each side.

Filters apply before pagination, and the cursor binds the active filter —
see [Cursor staleness](#cursor-staleness).

### `status="ok"` — short transcript, delivered whole

Returned when the (filtered) transcript fits: `mode="chunk"` was not requested,
the text is at most `YTT_INLINE_CHAR_LIMIT` chars (default `18000`), and no
`cursor` was passed. `is_final` is `true` and there is no `next_cursor`.

```json
{
  "video_id": "jNQXAC9IVRw",
  "status": "ok",
  "source": "caption_manual",
  "lang": "en",
  "transcript_quality": "human-authored captions",
  "text": "Okay, so here we are in front of the elephants …",
  "segments": [
    {"start": 0.0,  "duration": 2.0, "text": "Okay, so here we are in front of the elephants"},
    {"start": 2.0,  "duration": 3.0, "text": "The cool thing about these guys is …"}
  ],
  "title": "Me at the zoo",
  "channel": "jawed",
  "duration_sec": 19.0,
  "published": "20050424",
  "offset": 0,
  "total_chars": 133,
  "is_final": true
}
```

(`published` is the raw `YYYYMMDD` upload-date string; the example above is
abridged.) `source` is one of `caption_manual`, `caption_auto`, `whisper`,
and `transcript_quality` is the fixed human-readable string for that source —
`"human-authored captions"`,
`"auto-captions — may contain errors, no punctuation/speaker labels"`, or
`"ASR (Whisper) — may contain errors, no speaker labels"`.

### `status="partial"` — long transcripts, paginated

When the text does not fit inline (or `mode="chunk"` was sent), page 1 comes
back with a loud banner and a continuation cursor:

```json
{
  "video_id": "dQw4w9WgXcQ",
  "status": "partial",
  "source": "caption_auto",
  "lang": "en",
  "transcript_quality": "auto-captions — may contain errors, no punctuation/speaker labels",
  "text": "⚠️ PARTIAL: chars 1–18000 of 54120 (chunk 1/4). INCOMPLETE — call get_youtube_transcript again with cursor='Kq3mLm_2v8-xQ0fW1pZa9w:54120:18000' before summarizing, unless the user only needs the start.\n\nWe're no strangers to love …",
  "segments": [ {"start": 0.0, "duration": 0.5, "text": "We're no strangers to love"} ],
  "title": "…",
  "offset": 0,
  "total_chars": 54120,
  "is_final": false,
  "next_cursor": "Kq3mLm_2v8-xQ0fW1pZa9w:54120:18000"
}
```

Pagination mechanics:

- Chunks are cut at `YTT_CHUNK_CHARS` (default `18000`) Unicode characters,
  aligned to segment boundaries where possible, and never split mid-character.
  For non-Latin scripts the chunk shrinks (bytes/3 token budget) so one chunk
  cannot approach the context ceiling. The `chunk i/n` count in the banner is
  an estimate — segment alignment can shift boundaries.
- The banner is exactly `⚠️ PARTIAL: chars A–B of T (chunk i/n). INCOMPLETE — …`
  followed by a blank line. `A` is the 1-indexed first character of the chunk,
  `B` the exclusive end. Strip everything up to and including the first blank
  line to get the raw chunk text; the de-bannered chunks **concatenated
  directly** (no separator) reassemble the full transcript byte-for-byte.
- **Continue** by calling `get_youtube_transcript` again with the same `url`,
  the served `lang`, and `cursor` set to the returned `next_cursor`:

```json
{ "url": "https://youtu.be/dQw4w9WgXcQ", "lang": "en",
  "cursor": "Kq3mLm_2v8-xQ0fW1pZa9w:54120:18000" }
```

  The reply carries `offset=18000` and the next slice; repeat until a page
  arrives with `status="ok"` and `is_final=true` and no `next_cursor`.
- Cursors are opaque: `"<22-char hash>:<total_chars>:<offset>"`. Always pass
  them back unchanged.
- Continuing pages are served from the cache — no refetch, and no rate-limit
  token is spent anywhere in pagination.

### Cursor staleness

A cursor is bound to the content it was minted from: the hash inside it
encodes `(transcript content, lang, source, active filter args)`. It is
rejected with `status="error"`, `error_code="cursor_stale"` when:

- it was tampered with or is malformed (wrong shape, non-integer fields);
- its offset is out of range;
- it is carried to a **different video**;
- the video's cached transcript was **refreshed** (re-fetched) in the meantime;
- the cache unit was **evicted** between pages.

Never serve page 2 against different content — the server fails closed. The
message tells the model what to do:

```json
{
  "video_id": "dQw4w9WgXcQ",
  "status": "error",
  "error_code": "cursor_stale",
  "message": "Pagination cursor is stale — the transcript was refreshed or evicted. Re-call get_youtube_transcript without a cursor to restart pagination."
}
```

Restarting without the cursor yields a fresh page 1 (and, for filters, the
filter arguments must match the original call for its cursor to keep
validating).

### `status="pending"` — no captions, Whisper ASR started

If no caption track can be retrieved — the video has none, or the fetch failed
unrecognized — a Whisper ASR job is started automatically (get-or-create keyed
by `video_id` — concurrent callers join the in-flight job; the join is free):

```json
{
  "video_id": "VIDEOID11",
  "status": "pending",
  "eta_sec": 42.0,
  "message": "No captions found. Transcribing with Whisper ASR (~42s). Ask me again shortly."
}
```

`eta_sec` is `duration_sec × YTT_WHISPER_REALTIME_FACTOR` (default ×2.0) when
the video duration is known, otherwise `null`. **Relay the message/ETA to the
user and stop — do not poll in a loop.** Retrieve the result later with
`get_transcript_job`.

Boundaries enforced before any job is started:

| Condition | Response |
|---|---|
| Video longer than `YTT_MAX_ASR_DURATION_SEC` (default `1200` s) | `error`, `error_code="too_long_for_asr"` — no job, no quota spend |
| Backlog (pending + running jobs) at `YTT_MAX_PENDING_WHISPER_JOBS` (default `16`) | `error`, `error_code="rate_limited"`, message `Whisper queue full (…/… jobs pending or running). Try again later.` |
| Subject's new-job quota `YTT_WHISPER_JOBS_PER_HOUR` (default `10`/h) exhausted | `error`, `error_code="rate_limited"`, message `Whisper ASR quota exhausted (… jobs/hour per subject). Try again in ~Ns.` |

### Rate limits (what costs, what is free)

The per-subject fetch limiter (`YTT_RATE_LIMIT_PER_MIN`, default `20/min`,
burst = rate) is charged **only on the cache-miss fetch path** — failed
fetches spend a token too. **Cache hits, all pagination pages, and
`get_transcript_job` polls are free.** A denial looks like:

```json
{
  "video_id": "freshMiss01",
  "status": "error",
  "error_code": "rate_limited",
  "message": "Rate limit exceeded (20 requests/min per subject). Try again in ~12s."
}
```

`rate_limited` also covers capacity rejections (fetch pool full, Whisper queue
full) and a timed-out extraction attempt; the message distinguishes them and
carries the retry hint when one is known.

## Tool 2: `get_transcript_job`

Poll the status of a Whisper ASR transcription job. Signature:

```
get_transcript_job(video_id)
```

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `video_id` | str | *required* | The canonical 11-char id exactly as returned by the earlier `pending` response. |

Jobs move `pending → running → done | error`; terminal entries are garbage
collected after `YTT_JOB_TTL_SEC` (default `3600` s), and a `running` entry
stuck past `YTT_WHISPER_TIMEOUT_SEC` (default `2880` s) + `YTT_JOB_TTL_SEC` is
reaped the same way — a poll for a reaped job answers `not_found`. **Jobs are
private to the OAuth subject that started them** (`YTT_ALLOWED_SUBJECTS`
entry, matched on the same normalized key as the rate limit): a poll from any
other subject returns the same `not_found` an unknown id gets, so job ids
cannot be probed across subjects (pinned by
`tests/unit/test_job_ownership.py`). Polling states:

| Poll response | Meaning | What to do |
|---|---|---|
| `status="pending"`, `eta_sec` | Queued — waiting for a Whisper slot | Relay the ETA, stop; call again later |
| `status="running"`, `eta_sec` | Transcription in progress | Relay the ETA, stop; call again later |
| `status="ok"` (+ full transcript fields) | Done — the transcript is delivered **directly in this response** | Done; no further call needed |
| `status="error"`, `error_code`, `message` | The job failed; the job's own code is relayed | Re-call `get_youtube_transcript` with the original URL to retry |
| `status="error"`, `error_code="not_found"` | No such job — unknown id, expired/GC'd, started by a different OAuth subject, or the done transcript was evicted | Re-call `get_youtube_transcript` with the original URL |

```json
{"video_id": "VIDEOID11", "status": "pending", "eta_sec": 30.0,
 "message": "Transcription is queued. Estimated time: ~30s. Ask me again shortly."}
```

```json
{"video_id": "VIDEOID11", "status": "running", "eta_sec": 20.0,
 "message": "Transcription is in progress. Estimated time remaining: ~20s. Ask me again shortly."}
```

`done` delivers the same shape as `get_youtube_transcript` with `mode="full"`
— the ASR text, `source="whisper"`, `lang="whisper"`,
`transcript_quality="ASR (Whisper) — may contain errors, no speaker labels"`:

```json
{
  "video_id": "VIDEOID11",
  "status": "ok",
  "source": "whisper",
  "lang": "whisper",
  "transcript_quality": "ASR (Whisper) — may contain errors, no speaker labels",
  "text": "asr produced these words",
  "segments": [{"start": 0.0, "duration": 0.5, "text": "asr"}],
  "offset": 0,
  "total_chars": 24,
  "is_final": true
}
```

A **long** ASR transcript is delivered as `status="partial"` +
`next_cursor` exactly like any other long transcript; continue it by calling
`get_youtube_transcript` with the cursor (no `lang` needed — the `whisper`
cache unit satisfies any language).

A failed job relays the job's own error:

```json
{"video_id": "VIDEOID11", "status": "error", "error_code": "asr_failed",
 "message": "Whisper service error 503: …"}
```

```json
{"video_id": "UNKNOWNID01", "status": "error", "error_code": "not_found",
 "message": "Job not found. Re-call get_youtube_transcript with the video URL to start a new request."}
```

A `not_found` for a job that previously completed means the finished
transcript was evicted from the cache before it was polled; the dead registry
entry is cleaned up and the message says so (`…has been evicted…`).

## Stable error codes

`error_code` values are a stable taxonomy — match on them, not on `message`
text. `message` is always safe to relay verbatim.

| `error_code` | Emitted by | Meaning |
|---|---|---|
| `bad_url` | `get_youtube_transcript` | Not a single-video YouTube URL (playlist / channel / handle / search / unrecognized). |
| `private` | `get_youtube_transcript` | Video is private. |
| `members_only` | `get_youtube_transcript` | Members-only video. |
| `age_restricted` | `get_youtube_transcript` | Age-restricted — sign-in required. |
| `region_blocked` | `get_youtube_transcript` | Not available in the egress region. |
| `is_livestream` | `get_youtube_transcript` | Live stream / upcoming live event — never transcribed, never enters ASR. |
| `unavailable` | `get_youtube_transcript` | Video removed or otherwise unavailable. |
| `rate_limited` | both | Per-subject rate limit, ASR quota, Whisper queue full, fetch pool full, or a timed-out extraction. Message says which and carries the retry hint. |
| `ip_blocked` | both | YouTube blocked the egress IP (bot check / 403). Retried once through the residential proxy when `YTT_PROXY_URL` is set. |
| `empty_body` | `get_youtube_transcript` | yt-dlp's fallback classification: no info, unrecognized upstream failure — or an unexpected server fault (`message` starts `Unexpected error:`). On the fetch path this classification flows into the Whisper fallback (it surfaces as `status="pending"`, not as an error); it reaches the caller as an `error_code` only when a fault happens outside the normal fetch/ASR flow. |
| `too_long_for_asr` | both | Video exceeds `YTT_MAX_ASR_DURATION_SEC` (or the audio-size cap) — refused before any job starts; the caption path is unaffected. Via `get_transcript_job` when the download-time backstop catches it. |
| `asr_failed` | `get_transcript_job` | The Whisper job failed — service unreachable, 5xx, or timed out. Retry by re-calling `get_youtube_transcript`. |
| `not_found` | `get_transcript_job` | Unknown video id, job expired/GC'd, started by a different OAuth subject, or done-but-evicted transcript. |
| `cursor_stale` | `get_youtube_transcript` | Pagination cursor no longer valid — restart without the cursor. |

### `no_captions_asr_failed` is not a tool error code

`no_captions_asr_failed` (and its pair `no_captions_asr_started`) appear in
the plan and on metrics — but **neither is ever returned as a tool
`error_code`**. They label the `ytt_fetch_blocks_total` outcome series and
Whisper-job-internal bookkeeping. The tool-visible contract for a
caption-less video is:

1. `get_youtube_transcript` → `status="pending"` (no `error_code` field);
2. if ASR fails, `get_transcript_job` → `status="error"` with the job's real
   code — typically `asr_failed` (no/unreachable ASR endpoint), `ip_blocked`
   (audio download blocked), or `too_long_for_asr` (download-time backstop).

Older copies of the self-hosting guide showed `no_captions_asr_failed` in a
response body; that was wrong — see the table above for what actually
surfaces.

## Notes for client implementations

- **Match on `status` first**, then `error_code`. `ok` + `is_final=true` means
  the transcript is complete; `partial` always carries `next_cursor`;
  `pending`/`running` always carry a relayable `message`.
- Never paginate blindly: on `partial`, keep calling with `cursor=next_cursor`
  (plus the served `lang` and the same filter arguments) **before**
  summarizing, unless the user only needs the beginning.
- Never write your own cursor. The hash segment is content-derived; a guessed
  cursor is just `cursor_stale`.
- Treat every `message` as user-facing text: it is written to be relayed
  verbatim.
