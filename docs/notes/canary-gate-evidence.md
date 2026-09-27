# Canary-gate evidence artifacts — the contract

Bead `ytt-7f576b65`. The README promises that `ytt canary --gate`
"retains the JSON evidence, and prints the rollback/escalation directive on
failure"; `deploy/RUNBOOK.md` §3/§3.1 tell the operator what to *do* with it.
This note is the missing specification of the artifact itself — its schema,
where it lands, how long it lives, what failure output looks like, and what
must never appear inside it. The implementation is `ytt/canary_gate.py`; the
pinning tests are `tests/unit/test_canary_gate.py` (`TestEvidenceSpecDoc`
drift-guards this document against the code — the two rot together or not at
all). The §4 durability contract and its operator surfaces were added by
bead `ytt-958dccc1`.

## 1. The artifact at a glance

| Property | Contract |
|---|---|
| Format | One UTF-8 JSON object per gate run, `json.dumps(..., indent=2)` + trailing newline |
| Destination | `--evidence-dir <dir>`, else the default `/tmp/ytt-canary-evidence/` |
| Filename | `ytt-canary-gate-<YYYYMMDDTHHMMSSZ>.json` — the gate's UTC `ran_at`; a same-second re-run gets `-2`, `-3`, … appended, never an overwrite |
| Write | Atomic (temp file + rename) — a reader never observes a partial artifact |
| Retention | The gate **never deletes or prunes** evidence; each run appends a new file. Retention is the write-side guarantee only — what survives the *run environment* is the durability contract (§4): the release record is the durable copy |
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
could destroy it mid-incident (§4). To make an artifact outlive its run,
pass `--evidence-dir` naming storage that outlives it — §4's survival table
spells out what survives where, per run environment.

The filename embeds the gate's UTC `ran_at` (`YYYYMMDDTHHMMSSZ`), so
artifacts sort chronologically per directory. Two gate runs started in the
same second — the runbook's "re-run the gate once before acting" executed
immediately, or a scripted retry — would otherwise collide on one name and
the second write would silently destroy the first run's evidence. The second
artifact instead takes a numeric suffix (`...Z-2.json`, `...Z-3.json`, …);
every run's artifact survives, and each report's embedded `evidence_file`
names its own file. Sequential runs are the contract; concurrent gates
sharing one directory are not a supported scenario.

## 4. Retention and durability — what survives the run

Two guarantees that must not be conflated:

**Retention** is the gate's write-side behavior: the gate **never deletes,
prunes, or expires** evidence — there is no TTL, no cap, no startup sweep,
at *any* destination. Each run appends a new artifact; nothing removes an
old one. A persistent `--evidence-dir` therefore grows without bound by
design (artifacts are KB-scale); bounding, archiving, or pruning it is an
operator decision, never a gate behavior.

**Durability** — whether an artifact outlives the run — is decided by the
run environment, not by the gate. The gate cannot pick a default that
survives everywhere it runs: in an ephemeral CI pod **nothing inside the pod
survives completion** (iad-ci's controller runs `podGC: OnPodCompletion`),
so the durable copy has to be made *outside* the run environment no matter
where the artifact was written. What survives, per environment:

| Where the gate runs | The artifact survives | The durable copy |
|---|---|---|
| Server pod, `kubectl exec` (reference deployment); default `/tmp/ytt-canary-evidence/` on the container rootfs | Until the pod dies — the single replica swaps with `strategy: Recreate`, so every deploy destroys it | The operator's stdout capture (`tee`), pasted into the release bead |
| Server pod, `--evidence-dir` on storage that outlives the pod (PVC-backed, outside `YTT_CACHE_DIR`/`YTT_SCRATCH_DIR`) | Restarts and redeploys; artifacts accumulate unboundedly (retention, above) and stay cluster-local | Same — still not the release record |
| Self-hoster's host, default location | Until reboot or `/tmp` cleaning | The host copy, or the same stdout capture |
| Ephemeral CI pod (`podGC: OnPodCompletion`) | **Nothing** — the pod is deleted the moment the run finishes | Only what leaves the pod before it ends: the step's stdout, harvested into a workflow output parameter or artifact — the pattern `ytt-build` uses to record `tested-tag`/`tested-digest` past podGC |

The `--evidence-dir` convention follows: pass it when the artifact must
outlive the run *and* the run environment has storage that outlives the run
— never under `YTT_CACHE_DIR`/`YTT_SCRATCH_DIR` (§3), and the cache volume
is never the evidence home even outside the cache subtree: it is
size-capped, its contents are evicted by design, and the cache runbook's
exhaustion recovery treats wiping it as routine — evidence must be neither
subject to, nor a casualty of, a retention mechanism.

**The operator copy/backup step — the durability contract in practice:**
the release record is the durable copy. Capture the gate's stdout at run
time, assert the capture, and paste it into the release bead — pass or
fail. The capture must (a) exist, (b) be non-empty, and (c) parse as a gate
report (`.gate` present); the executable form of that assertion lives where
the step is performed: `deploy/RUNBOOK.md` §3 step 4 and
`deploy/DEPLOY-CHECKLIST.md` §5 (pinned to this contract by
`TestEvidenceDurabilityDoc`). A pass without retained evidence is an
unauditable release; a failure without evidence is an escalation nobody can
act on.

When the evidence write fails outright (read-only directory, disk full), the
gate degrades instead of failing: the report gains `evidence_error`, stdout
still carries the full JSON, and the `gate` verdict is never flipped by a
write problem — a red evidence directory must not sink a green release (or
resurrect a red one). The stdout capture is not degraded by it: the capture
is the evidence.

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
  side alone now fails CI instead of silently diverging;
- `TestEvidenceDurabilityDoc` (bead `ytt-958dccc1`) drift-guards §4's
  durability contract against the operator surfaces that enact it: the
  capture-and-assert step must stay in `deploy/RUNBOOK.md` §3 step 4 and
  `deploy/DEPLOY-CHECKLIST.md` §5 — same assertion, failure branch
  included — the README's "retains the JSON evidence" promise must keep
  pointing at this contract rather than implying an artifact is safe
  because it exists, and one leg replays the checklist's assertion against
  the gate's real stdout so the documented command asserts something true;
- `tests/unit/test_canary_once.py` (`TestReadmeVerdictDoc`, bead
  `ytt-066781cf`) drift-guards the README's `ytt canary --once` verdict line
  against this section's outcome definition: the documented vocabulary must
  stay `"ok"` or a stable `ytt.errors` error code, with the taxonomy as the
  source of truth — never a relapse into a closed `ok` vs `ip_blocked` pair,
  and never an example code the canary cannot actually report.
