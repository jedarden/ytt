# Canary-gate evidence artifacts — the contract

Bead `ytt-7f576b65`. The README promises that `ytt canary --gate`
"retains the JSON evidence, and prints the rollback/escalation directive on
failure"; `deploy/RUNBOOK.md` §3/§3.1 tell the operator what to *do* with it.
This note is the missing specification of the artifact itself — its schema,
where it lands, how long it lives, what failure output looks like, and what
must never appear inside it. The implementation is `ytt/canary_gate.py`; the
pinning tests are `tests/unit/test_canary_gate.py` (`TestEvidenceSpecDoc`
drift-guards this document against the code — the two rot together or not at
all).

## 1. The artifact at a glance

| Property | Contract |
|---|---|
| Format | One UTF-8 JSON object per gate run, `json.dumps(..., indent=2)` + trailing newline |
| Destination | `--evidence-dir <dir>`, else the default `/tmp/ytt-canary-evidence/` |
| Filename | `ytt-canary-gate-<YYYYMMDDTHHMMSSZ>.json` — the gate's UTC `ran_at`; a same-second re-run gets `-2`, `-3`, … appended, never an overwrite |
| Write | Atomic (temp file + rename) — a reader never observes a partial artifact |
| Retention | The gate **never deletes or prunes** evidence; each run appends a new file. Durability beyond the filesystem it runs on is the operator's duty (§4) |
| Secrets | The artifact contains no credentials and no sensitive configuration (§6) |

## 2. Schema

Top-level keys (exact set, pinned by `TestGateReportSchema`):

| Key | Type | Meaning |
|---|---|---|
| `mode` | `"gate"` | Discriminates the combined report from a `--once` report |
| `ran_at` | ISO-8601 UTC, second resolution | Gate start time; also the filename timestamp source |
| `video_id` | string | The video every probe fetched (the first `CANARY_VIDEO_IDS` entry unless `--video-id`) |
| `proxy_configured` | bool | Whether `YTT_PROXY_URL` is set — i.e. whether a `via_proxy` probe exists |
| `probes` | object | One `run_once`-shaped report per probe, keyed `direct` and (only when `proxy_configured`) `via_proxy` |
| `verdict` | string | The first failing probe's outcome, else `"ok"` |
| `gate` | `"pass"` \| `"fail"` | The release decision — `"pass"` only when **every** probe's `verdict` is `ok` |
| `failed_probe` | `null` \| `"direct"` \| `"via_proxy"` | First failing probe in run order; `null` on a pass |
| `remediation` | `null` \| string | The rollback/escalation directive (`remediation_for`); `null` on a pass. Mirrors `deploy/RUNBOOK.md` §3.1 — the two are updated together |
| `evidence_file` | `null` \| string | Absolute path of the artifact; the value embedded **inside** the file equals the path it was written to |

One conditional key: `evidence_error` (string) appears **only** when the
evidence write failed (§4) — the report then exists only as stdout, and the
`gate` verdict is untouched by the write failure.

Each entry of `probes` keeps the full `ytt canary --once` shape (pinned by
`tests/unit/test_canary_once.py`): `mode` (`"once"`), `ran_at`, `video_id`,
`egress`, `caption_fetch`, `verdict`.

- `egress` is the ipinfo classification context: `ip`, `asn`, `org`,
  `via_proxy`, `is_residential` — or, when the ipinfo probe itself failed,
  `error` (credential-redacted) plus `via_proxy`. It is context, never the
  verdict: the gate passes or fails on `caption_fetch` alone.
- `caption_fetch` carries:

| Key | Meaning |
|---|---|
| `ok` | bool — caption fetch succeeded |
| `outcome` | `"ok"` or a stable `ytt.errors` error code, or `gate_error` when the probe crashed before completing (§5) |
| `langs` | list of caption language codes |
| `duration_sec` | float — probe wall time |
| `via_proxy` | bool — whether *this* probe dialed through `YTT_PROXY_URL` |
| `error` | `null`, or a credential-redacted failure string |

`gate_error` is synthesized by the gate itself when `run_once` raises: the
report keeps the probe shape (`ok: false`, `outcome: "gate_error"`) so a
crashing probe can never be mistaken for a passing one, and the error string
is credential-redacted.

## 3. Destination and naming

`--evidence-dir` (CLI, gate mode only) names the directory; it is created
recursively when missing. Without it the destination is
`/tmp/ytt-canary-evidence/` — writable in every context the gate runs (server
pod, canary pod, self-hoster's host), and deliberately **not** under
`YTT_CACHE_DIR`/`YTT_SCRATCH_DIR`: those are swept or evicted by design, and
an evidence artifact must never be subject to a retention mechanism that
could destroy it mid-incident (§4).

The filename embeds the gate's UTC `ran_at` (`YYYYMMDDTHHMMSSZ`), so
artifacts sort chronologically per directory. Two gate runs started in the
same second — the runbook's "re-run the gate once before acting" executed
immediately, or a scripted retry — would otherwise collide on one name and
the second write would silently destroy the first run's evidence. The second
artifact instead takes a numeric suffix (`...Z-2.json`, `...Z-3.json`, …);
every run's artifact survives, and each report's embedded `evidence_file`
names its own file. Sequential runs are the contract; concurrent gates
sharing one directory are not a supported scenario.

## 4. Retention

The gate **never deletes, prunes, or expires** evidence — there is no TTL,
no cap, no startup sweep. In the default location that means artifacts die
with the pod's `/tmp`: that is expected, not a leak, because the default
location is a convenience copy. The durable copy is the operator's duty
(`deploy/RUNBOOK.md` §3): `kubectl exec … -- ytt canary --gate | tee
canary-gate-<ts>.json`, and the `tee`d stdout copy — or the artifact itself
where the filesystem survives — is pasted into the release bead, pass or
fail. A pass without retained evidence is an unauditable release; a failure
without evidence is an escalation nobody can act on.

When the evidence write fails outright (read-only directory, disk full), the
gate degrades instead of failing: the report gains `evidence_error`, stdout
still carries the full JSON, and the `gate` verdict is never flipped by a
write problem — a red evidence directory must not sink a green release (or
resurrect a red one).

## 5. Failure output and exit codes

| Channel | Pass | Fail |
|---|---|---|
| stdout | The full JSON report, verbatim (`json.dumps(..., indent=2)`) | The same full JSON report — the failure detail lives in the report, not only in the banner |
| stderr | empty | `CANARY GATE FAILED: <verdict> (probe: <failed_probe>, video <video_id>)`, then the `remediation` directive on its own line |
| exit code | `0` | `1` |

Exit `2` is argparse usage rejection only (`--gate` with `--once`, `--gate`
with `--via-proxy`, `--evidence-dir` without `--gate`, `--video-id` without
`--once`/`--gate`). Any outcome the gate could not classify is a *failure*,
not a crash: a probe that raises is reported as `gate_error` with exit `1`,
because a tooling/environment failure must block a release the same way a
real egress failure does — it just tells the operator not to read it as an
egress verdict (RUNBOOK §3.1, last row).

The stdout report and the on-disk artifact are the same object: consumers
may parse either. `report.evidence_file` is the only sanctioned way to find
the artifact — the filename timestamp is an implementation detail, not an
interface.

## 6. Sensitive-configuration exclusion

The artifact is pasted into release beads and read by people who should not
have to hold credentials. It must never contain:

- the `YTT_PROXY_URL` credentials — its `user:password@` userinfo (the
  residential-proxy credential) never appears, in any field, in any output
  channel.  What *may* appear in a redacted error string is the proxy's
  host:port (`dial http://proxy.example.com:3128 failed`) — `redact_credentials`
  strips the userinfo and keeps the address, matching the fetch path's
  logging contract;
- the OAuth client secret (`YTT_OAUTH_CLIENT_SECRET`) or any bearer token —
  the gate never touches them, and nothing would transport them into a probe
  report;
- session cookies or video URLs beyond the canary video ID.

Every free-text string that could quote the proxy URL (yt-dlp/httpx
exception text) passes through `redact_credentials` twice: once where the
exception is caught (`ytt.canary.run_once`, `probe_once_detail`), and once
more at the artifact boundary (`canary_gate._scrub_secrets` walks every
string leaf of every probe report before it is embedded). The second pass is
defense-in-depth: the gate owns the artifact, so it does not trust an
upstream layer to have redacted. Both passes only rewrite
`scheme://user:password@host` shapes — legitimate content is untouched.

What the artifact deliberately *does* carry: the egress classification
(`ip`, `asn`, `org`, `is_residential`) from the `--once` probe — that is the
proof of residential egress, the thing the gate exists to retain. Note for
operators: that means a release bead containing gate evidence contains the
pod's egress IP. That is observed state, not configuration, and it is the
evidence; redacting it by default would gut the artifact. Redact it by hand
only if the release record's audience makes that necessary.

## 7. Pinning tests

`tests/unit/test_canary_gate.py` holds this contract from both sides:

- schema/retention/secret tests drive the real `run_gate` + `_write_evidence`
  with mocked probes and assert on the **on-disk artifact**, not just the
  returned report;
- the failure-output table is pinned at the CLI layer (stdout JSON, stderr
  banner + directive, exit 0/1/2);
- `TestEvidenceSpecDoc` drift-guards this document: every key, literal, and
  guarantee claimed above must still be true of the code, and every key the
  code emits must still be documented here — the doc and the code rot
  together or not at all;
- `TestRunbookRemediationMirror` (bead `ytt-fefb4698`) drift-guards the other
  mirror this note only used to record: every decision-bearing phrase
  `remediation_for` emits must still be legible in `deploy/RUNBOOK.md` §3.1's
  decision table, and §3.1 must keep naming the function — an edit to either
  side alone now fails CI instead of silently diverging.
