# Derived-URL policy — what yt-dlp metadata may make us dial

`docs/notes/input-security.md` closes the *caller-input* surface: the free-form
string a model submits is parsed, never dialed, and every YouTube-bound call
site rebuilds a literal watch URL from the validated 11-char id. But the
transcript paths do not stop at that first dial. yt-dlp's metadata response
contains further URLs the code goes on to follow, and those strings are
attacker-influenced to exactly the degree the metadata response is — a
compromised or lying extractor answer could point this service's egress at
localhost, the private network, or a cloud-metadata endpoint. This note is the
single spec for that second surface: which metadata-derived URLs may be
dialed, how redirects are contained, and exactly what a rejection looks like.

Pinned by `tests/unit/test_derived_url.py`.

## The derived-URL surface

| Derived URL | Produced by | Dialed by |
|---|---|---|
| json3 caption-track URL (`…/api/timedtext?…`) | `extract_info` metadata (`subtitles` / `automatic_captions`) | `ytt.fetch._do_fetch` via `ydl.urlopen` |
| media / manifest format URLs (`…googlevideo.com/videoplayback?…`, HLS/DASH manifests, fragment URLs) | `extract_info` metadata (`formats`) | `ytt.whisper._do_download_audio` via `ydl.download` |
| every redirect target reached from either | the HTTP response chain | yt-dlp's request backends, per hop |

The initial watch URL itself is *not* on this surface — it is the literal
template over the validated id (input-security §The output invariant).

## The policy

A derived URL may be dialed only if **all** of the following hold. The URL is
**validated, never rewritten** — what was checked is what gets dialed.

1. **String shape** — non-empty, a `str` (not `bytes`/`None`/an object).
2. **Parseable** — `urllib.parse.urlparse` must accept it. The stdlib refuses
   some shapes outright (unbalanced `[`, NFKC-normalizing host delimiters,
   unparseable/out-of-range ports); as in `canonicalize`, that translates to a
   rejection rather than a leaked `ValueError`. Fail closed.
3. **Scheme is exactly `https`** (case-insensitive). Production egress is
   TLS-only; a metadata response offering `http` — a plaintext downgrade of
   the very stream we are fetching — or `file`/`data`/`ftp`/`javascript` is a
   violation, not a compatibility problem.
4. **No `user:pass@` userinfo.** Derived URLs never legitimately carry
   credentials, so smuggling resolves in the safe direction: `evil.com@rr3---….googlevideo.com` is rejected, not accepted.
5. **No explicit port** — not even `:443`. YouTube never serves a nonstandard
   port, and an "allowlisted host on port 8443" has no legitimate metadata
   origin.
6. **Host is on the allowlist** (lowercased, no userinfo/port, as `urlparse`
   reports it):
   - suffix-anchored: `youtube.com`, `youtube-nocookie.com`,
     `googlevideo.com`, `ytimg.com` — `rr3---sn-abc.googlevideo.com` matches;
     `youtube.com.evil.com`, `evilmoogle.com` and trailing-dot
     `youtube.com.` do not;
   - exact: `youtubei.googleapis.com` — the InnerTube API host the pinned
     player clients (`tv`/`web_embedded`/`mweb`) talk to. `googleapis.com` as
     a whole is far too wide to suffix-match.

Because the rule is an allowlist rather than a blocklist, the classic SSRF
target set dies on rule 6 without special-casing: `localhost`, `127.0.0.1`,
`[::1]`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `169.254.169.254`
(cloud metadata), `169.254.170.2` (ECS), and every decimal/hex/octal IP-literal
spelling are just non-allowlisted hosts. There is no DNS resolution in the
gate: rejection is decided by `urlparse` and set membership, offline.

## Enforcement — three layers

All three delegate to one function, `ytt.derived_url.validate_derived_url`.

1. **Pre-dial validation, caption path** — `ytt.fetch._do_fetch` validates the
   selected json3 track URL after `_select_track` and before `ydl.urlopen`.
   A violation raises before any byte is dialed.
2. **Pre-download audit, audio path** — `ytt.whisper._do_download_audio`
   calls `audit_audio_info_urls(info)` after the duration/size caps and before
   `ydl.download`. It validates every URL in the info dict the downloader is
   about to resolve: top-level `url`/`manifest_url`, and per-format `url`,
   `manifest_url`, `fragment_base_url`, and absolute per-fragment URLs. Bare
   relative fragment paths are skipped (they resolve against an
   already-validated base and cannot change host); protocol-relative
   `//host/…` fragments are *not* skipped — they change host exactly like an
   absolute URL and are audited in their `https:` form.
3. **Network-layer gates, process-wide** — `ytt.derived_url.install()`, armed
   once at `ytt.fetch` import, wraps three yt-dlp hooks (internals — a
   maintenance point pinned like `SEED_MAP`, see below):
   - `yt_dlp.YoutubeDL.urlopen` — the single choke point every extractor and
     native downloader dial passes through. This also covers the *second*
     metadata extraction `ydl.download([url])` performs internally, whose
     result no caller ever sees — the audit in layer 2 cannot, so this gate
     is what makes the audio-path containment bind the actual dials.
   - `RedirectHandler.redirect_request` (urllib backend — the backend this
     project runs, `requests` is not installed) — every redirect hop within
     one dial, validated before the hop is sent.
   - `RequestsSession.rebuild_method` (requests backend) — the same per-hop
     gate for the other backend, armed only when `requests` is installed.

   Redirect containment matters because each hop is a fresh dial decided from
   response data: a `timedtext` URL that answers `302 → https://169.254.169.254/…`
   is rejected at the hop, not after following it.

## Rejection behavior (what a caller sees)

- **`error_code` is the stable `"bad_metadata_url"`** (`ytt.errors.BAD_METADATA_URL`)
  — never `bad_url` (the caller's input was fine; there is nothing for the
  calling model to correct) and never `empty_body` (that code is the trigger
  for the Whisper ASR fallback — routing a metadata violation into ASR would
  download audio from the very video whose metadata misbehaved). The server's
  Whisper fallback keys on `empty_body` only, so `bad_metadata_url` takes the
  plain-error branch: no job, no quota charge, no audio.
- **The message** reads
  `metadata URL rejected: <what> URL is not on the allowed scheme/host policy (<reason>): '<redacted url>'`
  — the marker (`metadata URL rejected`, `ytt.derived_url.POLICY_VIOLATION_MARK`),
  which surface produced the URL (`caption track`, `audio download`,
  `redirect target`, `yt-dlp request`), the policy rule that failed, and the
  offending URL credential-redacted (`observability.redact_credentials`).
  Tool responses are authenticated-caller content, so quoting the URL is the
  input gate's own convention (input-security §Error contract).
- **When the violation surfaces through yt-dlp** (a redirect hop mid-download,
  a dial inside the second extraction) the gates raise it as a yt-dlp
  `RequestError` carrying the marker in its message. yt-dlp wraps that as a
  `DownloadError`/`ExtractorError` like any network failure, and
  `ytt.fetch.classify_ydl_error` maps the marker — pinned as the **first**
  `SEED_MAP` entry, ahead of every generic seed — back to
  `bad_metadata_url`. Both fetch paths catch raw `RequestError` as well, so
  the classification holds whichever wrapper yt-dlp chooses.
- **The canary** (`ytt.canary`) classifies every probe failure through the
  same `classify_ydl_error`, so a metadata violation on the ladder reports
  `outcome: "bad_metadata_url"` instead of a misleading `empty_body`.

## Pinned yt-dlp internals (maintenance point)

Like `SEED_MAP`, the gates reach into yt-dlp internals and are pinned to the
`yt-dlp` version in `pyproject.toml` (currently `2026.8.19`):

- `yt_dlp.YoutubeDL.urlopen` (public, stable signature: `str | networking.Request`);
- `yt_dlp.networking._urllib.RedirectHandler.redirect_request`;
- `yt_dlp.networking._requests.RequestsSession.rebuild_method` (optional
  backend — absent here, its gate is skipped by design);
- `YoutubeDL._request_director` and the handlers' `_make_sslcontext` — not a
  gate, but the TLS-verification backstop §Residual risk rests on: leg J
  builds the real director over this project's options and asserts every
  handler's context does hostname verification with `CERT_REQUIRED`. A bump
  that renames either fails loudly here rather than letting the acceptance
  silently lose its second leg.

On a yt-dlp bump, verify all three: a renamed hook fails loudly (AttributeError
at `ytt.fetch` import, or a failed identity assertion in
`tests/unit/test_derived_url.py`) rather than silently unguarding the
process.

## Known limitations (deliberate scope)

- **External downloaders** (ffmpeg/aria2c) bypass `ydl.urlopen`. None is
  configured: the paths use yt-dlp's native downloaders only.
- **The allowlist is YouTube-controlled** by construction: metadata says where
  the media lives; the policy says it must live on a YouTube-controlled host
  over TLS. Widening it is a code review event, not a config change — there is
  deliberately no environment knob.

## Residual risk — DNS rebinding on allowlisted hosts: ACCEPTED

The gate is name-based by construction (§The policy: "There is no DNS
resolution in the gate"), so it constrains what a derived URL *names*, not
what that name *resolves to* at dial time. An allowlisted name — say
`rr3---sn-x.googlevideo.com` — resolving through hostile or poisoned DNS to
loopback, an RFC1918 address, or the cloud-metadata endpoint would pass all
six rules and be dialed at the poisoned address. That is this surface's one
residual, and it is **accepted deliberately** (decision bead `ytt-e1036a3b`,
2026-09-27 — the recorded-decision shape of http-endpoints.md's accepted
metrics exposure, `ytt-8303946b`, and auth.md's declined WAF allowlist,
`ytt-761fb151`). Pinned by `tests/unit/test_derived_url.py` leg J.

The acceptance rests on the attack needing *both* of two independent legs:

1. **Resolution control** — make the allowlisted name resolve where the
   attacker wants (compromised resolver, cache poisoning). Real, and
   invisible to a name-based gate.
2. **Certificate possession** — whatever answers at the poisoned address
   must complete the TLS handshake *for the allowlisted name*. The process
   dials with certificate verification armed (`check_hostname=True`,
   `CERT_REQUIRED` on every request handler yt-dlp builds for this
   project's options — asserted against the live director by leg J — and
   nothing in this repo sets `nocheckcertificate`) and on port 443 (rule 5
   allows no other). The cloud-metadata endpoints cannot clear this leg at
   all: they are plaintext-HTTP services on port 80, with certificates for
   their own names if any, never for `*.googlevideo.com`.

Leg 2 collapses the classic rebinding escalation. The allowlist is closed
under attacker registration: every entry lives under a registry YouTube
controls, so no domain-validation path can hand an attacker a legitimate
certificate for any allowlisted name. Holding one requires CA misissuance
for a Google-controlled name or compromise of Google's own keys/infra — at
which point the attacker can already impersonate YouTube *content* (serve a
lying json3 body or media stream from a public address they own), a
strictly broader compromise than steering the same dial into private
address space. The SSRF corner is the least of that incident, and the
resulting trust is the same one the input gate extends to
`https://www.youtube.com/watch?v=<id>` on every request — and every HTTPS
client on the internet extends to every name it dials. Note also what the
poisoned dial would even be worth: the dials carry no ytt credentials
(cookies are disabled, §The policy rule 4 bans userinfo), so the target
would have to be an internal service reachable from the pod — exactly the
target set leg 2 walls off.

**Why the mitigation was declined.** A post-resolution IP check at
`validate_derived_url` — the one choke point all three layers delegate to —
was considered and rejected:

- *It is check-then-dial TOCTOU theater.* The gate's resolution and the
  dial's resolution are two independent lookups; pinning the actual peer
  requires resolving in the gate and forcing the connection onto that IP —
  a dial *rewrite*, which this policy forbids by construction (§The policy:
  "validated, never rewritten") — or hooking the request backends' socket
  layer, a deeper pinned-internals surface (§Pinned yt-dlp internals) on a
  fast-moving dependency.
- *It breaks the gate's deliberate shape* — pure, offline, a pure function
  of the string (pinned). The audio audit validates every format and
  fragment URL, dozens per request, most of which are never dialed; every
  validation would become a DNS side effect, and resolver flakiness would
  masquerade as `bad_metadata_url` or demand a new error path.
- *It would not bind the documented proxy fallback.* With `YTT_PROXY_URL`
  set (`docs/notes/proxy-egress.md`) the *proxy's* resolver answers the
  dial, so a local
  pre-dial check would validate a different resolution than the one that
  matters. Production runs proxy-unset by design, but the fallback is a
  supported posture — and through a CONNECT tunnel the TLS backstop above
  holds unchanged, which is the symmetry that makes acceptance stable.

**Revisit triggers:** widening the allowlist to any name an attacker can
register under — a shared-hosting/public-suffix-style entry would hand them
a legitimate certificate and re-open leg 2 (`test_allowlist_shape_is_pinned`
is the tripwire); or arming an external downloader that dials without the
TLS backstop (§Known limitations). Otherwise the residual is recorded here
and carried.
