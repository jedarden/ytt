# Reference Whisper ASR contract

This note specifies what an unset `YTT_WHISPER_URL` means. It is the
reference for the default-ASR path; the explicit empty-value caption-only
behavior is covered by the no-Whisper tests and documentation. The
OpenAI-compatible wire format itself remains the BYO Whisper contract.

## Default endpoint and provenance

When `YTT_WHISPER_URL` is absent, `ytt.config.DEFAULT_WHISPER_URL` resolves
it to:

```text
http://whisper-openai.whisper-stt.svc.cluster.local:8000
```

This is the project-operated reference Whisper service on the reference
cluster. It is an in-cluster network endpoint, not a Whisper model bundled in
the `ytt` image and not a managed or third-party YouTube transcript API. The
default is intentionally useful for the reference deployment; it is not a
promise that this cluster-local hostname is reachable from a generic
self-hosted installation.

The setting is operator-overridable. Set `YTT_WHISPER_URL` to the base URL of
an operator-selected OpenAI-compatible service, or set it to the empty string
for explicit caption-only operation. An unset value and an empty value are
different configurations:

| `YTT_WHISPER_URL` | Meaning |
|---|---|
| absent | Use the project-operated reference endpoint above. |
| non-empty | Send ASR work to the operator-selected endpoint. |
| empty | Do not have a routable ASR endpoint; caption-less work follows the explicit no-Whisper failure path. |

## Tool-call and endpoint behavior

The first `get_youtube_transcript` call never blocks on Whisper and never
probes the endpoint to decide whether to create a job. If the caption fetch
finds no captions, it creates or joins a job and returns:

```json
{
  "video_id": "VIDEOID11",
  "status": "pending",
  "eta_sec": 42.0,
  "message": "No captions found. Transcribing with Whisper ASR (~42s). Ask me again shortly."
}
```

There is no inline transcript on that first call, even when the reference
service is healthy. The job downloads the video's audio, then posts it to
`{YTT_WHISPER_URL}/v1/audio/transcriptions`; `get_transcript_job(video_id)` is
the later poll. A successful poll returns the transcript inline (or the normal
pagination shape for a long transcript). The model guard may issue
`GET /v1/models` during startup, but its failure is fail-soft and does not
block boot.

The stable failure taxonomy is endpoint-independent:

| Failure | Caller-visible result |
|---|---|
| Reference endpoint unreachable, DNS/connect failure, timeout, malformed response, or non-2xx rejection | Initial `pending`; poll `status="error"`, `error_code="asr_failed"`. The message is safe to relay and includes the retry instruction. |
| Audio download blocked by YouTube | The job's `ip_blocked` (or the downloader's other stable fetch code); `YTT_PROXY_URL` may get its one YouTube retry. |
| Duration/audio-size cap before transcription | `too_long_for_asr`; no ASR job is started. |
| Per-subject quota or global ASR backlog cap | `rate_limited`; joining an existing job and polling remain free. |
| Explicit empty value | Initial `pending`; the job has no routable ASR endpoint and terminates as `asr_failed`. No HTTP(S) Whisper endpoint is dialed. |

There is no separate `reference_whisper_down` error code. An outage or a
reference service that rejects work is `asr_failed`, just like the same
failure from an overridden endpoint. Failed jobs are not cached; callers retry
by calling `get_youtube_transcript` again with the original URL.

`no_captions_asr_started` and `no_captions_asr_failed` are metrics-only and
Whisper-job-internal labels. They are not members of the tool `error_code`
taxonomy and must never replace the caller-visible `asr_failed` code above.

## Controls that apply unchanged

Reference-ASR jobs use the same controls as BYO-ASR jobs:

- `YTT_WHISPER_JOBS_PER_HOUR` charges each subject only for a new job. A
  reference endpoint outage still consumes that slot; joining an in-flight job
  is refunded/free, and `get_transcript_job` polls never charge quota.
- `YTT_PROXY_URL` is isolated from Whisper HTTP. It can be used for the
  YouTube audio download's direct-first, one-`ip_blocked` retry, but the
  reference ASR POST and the startup model guard are never sent through that
  proxy. This is the same boundary specified in
  [proxy-egress.md](proxy-egress.md).
- Job handles remain bound to the OAuth subject that created them. Another
  subject sees `not_found`, even when both subjects share the same in-flight
  work or cached result; see [auth.md § Job ownership](auth.md#job-ownership--whisper-asr-handles-are-per-subject).
- The same lifecycle applies: `pending → running → done|error`, terminal
  handles live for `YTT_JOB_TTL_SEC` (default `3600` seconds), and running
  zombies are reaped after `YTT_WHISPER_TIMEOUT_SEC + YTT_JOB_TTL_SEC`.
  Registry entries and in-flight tasks are lost on restart, cached transcripts
  survive on the PVC, and the scratch directory is swept on boot. A caller
  recovers from restart or expiry by re-calling `get_youtube_transcript`.

## Audio-egress disclosure and the no-third-party claim

“No third-party transcript APIs” means that `ytt` does not send a YouTube URL
to a managed transcript provider: captions are fetched in-server with
`yt-dlp`, and no managed transcript SDK or provider is part of the runtime.
It does **not** mean that every byte stays in the `ytt` process. On the
default caption-less path, `ytt` downloads audio from YouTube and sends that
audio over the network to the project-operated reference Whisper endpoint
above. Self-hosters must treat that as default audio egress and choose one of
these deliberate policies:

1. set `YTT_WHISPER_URL` to an endpoint they operate or otherwise trust;
2. set it to the empty value for caption-only operation; or
3. leave it unset only when sending audio to the project-operated reference
   service is acceptable and the reference hostname is reachable from the
   deployment.

The README and self-hosting guide repeat this disclosure. The regression
guards in `tests/unit/test_reference_asr_contract.py` and
`tests/unit/test_egress_boundary.py` keep the reference default, its provenance,
the two environment shapes, and the scoped no-third-party claim aligned.
