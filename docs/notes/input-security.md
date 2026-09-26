# Input security contract — untrusted input never reaches the network

`get_youtube_transcript(url, …)` takes a free-form string authored by a
model. That string is **parsed, never dialed**: `ytt.canonicalize.canonicalize`
reduces it to a bare 11-character video id in-process, and every rejection is
decided from string properties alone — before the cache, before the rate
limiter, before any `YoutubeDL` construction, before any socket use. This
note is the single spec for what is accepted, what is rejected, and why that
ordering makes SSRF structurally impossible rather than merely filtered.

Pinned by `tests/unit/test_input_security_contract.py` (this contract),
`tests/unit/test_canonicalize.py` (accepted forms + idempotence), and
`tests/unit/test_deletion_runbook.py` §3 (the operator URL→id table is
executed against the real function so it cannot drift).

## The output invariant — what ever reaches yt-dlp

yt-dlp is never handed the caller's string. Every YouTube-bound call site
builds its request from the *validated id* via the same literal template:

| Call site | URL passed to `extract_info` |
|---|---|
| Caption extraction — `ytt.fetch._do_fetch` | `f"https://www.youtube.com/watch?v={video_id}"` |
| Whisper audio download — `ytt.whisper` | same literal |
| Caption canary — `ytt.canary.probe_once_detail` | same literal; ids are the compile-time `CANARY_VIDEO_IDS` constants, never caller input |

`video_id` reaches those f-strings only after matching `VIDEO_ID_RE`
(`^[A-Za-z0-9_-]{11}$`). The host is therefore a compile-time constant and
the only interpolated part is a proven 11-char `[A-Za-z0-9_-]` string — no
metacharacter, whitespace, unicode lookalike, or userinfo payload can alter
the scheme, host, port, or path, or add a query parameter. The caller's URL
is not *sanitized*; it is **discarded** and the id is re-embedded into a
fresh, fully literal URL. (Only the caption path additionally opens a second
URL — the json3 track URL — which yt-dlp itself extracted from that same
canonical video's metadata, not from caller input.)

## The gate's position in the request path

`get_youtube_transcript` (`ytt/server.py`) runs, in order:

1. **canonicalize** — pure string work; a `YttError` here returns the
   `bad_url` error response immediately;
2. cache lookup (keyed by the canonical id);
3. per-subject rate limit (charged only on this cache-miss path);
4. fetch — the first code that could touch a socket.

So a rejected input costs nothing: no DNS lookup, no connection, no yt-dlp
work, no cache write, no quota charge. Rejection is decided entirely by
`re`, `urllib.parse`, and set membership.

## Input classes

### 1. Malformed input → `bad_url`

Empty or whitespace-only strings, a scheme with no host (`https://`), a
single-slash scheme (`https:/youtube.com/...`), backslash "netlocs"
(`https:\\evil.com\\...`), and inputs with embedded CRLF (header-injection
shapes) are all rejected. Nothing is repaired or rescued: the canonicalizer
either recognizes a video or fails loudly with a reason.

**The gate fails closed when the stdlib parser itself refuses the input.**
`urlsplit` raises `ValueError` for some malformed shapes — an unbalanced
bracket (`"Invalid IPv6 URL"`) and netloc characters that NFKC-normalize into
URL delimiters (e.g. a fullwidth `／` smuggling a path separator into the
host). `canonicalize` translates those into `bad_url` like any other
rejection; a raw `ValueError` never escapes the gate into the tool path.

### 2. Non-YouTube hosts → `bad_url` (the allowlist is exact)

After lowercasing, stripping one leading `www.` / `m.` / `music.` /
`gaming.` prefix, dropping any `user:pass@` userinfo, and dropping the port,
the remaining host must be **exactly** one of `youtube.com`,
`youtube-nocookie.com`, or `youtu.be`. One prefix strip, set membership —
not a substring or suffix match. All of these are rejected:

- lookalikes and supersets: `evilyoutube.com`, `notyoutube.com`,
  `youtube.com.evil.com`, `youtu.be.evil.com`, `m.youtube.com.com`,
  `youtube.com.` (trailing dot), `youtube-nocookie.com.evil.com`;
- non-YouTube sites that happen to carry a video id: `example.com`,
  `google.com`, punycode (`xn--…`) hosts;
- **IP literals and internal targets**: `127.0.0.1`, `[::1]`,
  `169.254.169.254` (cloud metadata), `localhost` — the classic SSRF target
  set is just another non-allowlisted host and dies here without a single
  packet.

**Userinfo smuggling is resolved in the safe direction.** The host is taken
from after the *last* `@`, so `https://youtube.com@evil.com/watch?v=ID`
parses as host `evil.com` → rejected. The reverse,
`https://evil.com@youtube.com/watch?v=ID`, has host `youtube.com` and is
accepted — `evil.com` was the userinfo component, which is dropped; nothing
derived from it is ever used. Credentials on an accepted URL
(`https://user:pass@youtube.com/watch?v=ID`) are likewise ignored: the
output is the bare id only. All of this is safe because the input URL is
never a dial target anywhere in the codebase (see the output invariant).

### 3. Redirecting URLs → `bad_url`; redirects are never resolved

- **URL shorteners** (`bit.ly`, `t.co`, `goo.gl`, …): any non-allowlisted
  host is rejected outright. Resolving a redirect means dialing the
  untrusted input — exactly what this gate exists to avoid — so a shortener
  is a loud `bad_url`, never a chase.
- **YouTube's own redirectors**: `/attribution_link?u=…` and
  `/redirect?q=…` are unrecognized paths → `bad_url`. The `u=`/`q=` target
  is never parsed, decoded, or followed — even when it points at a perfectly
  canonical `youtube.com/watch?v=ID` URL. An accepted URL must name the
  video *directly*; there is no indirection escape hatch.
- **Redirectors on other hosts** (`google.com/url?q=…`): non-allowlisted
  host → `bad_url`.

The tool description tells models to pass YouTube URLs directly
(`README`, plan §Tools), so a shortener reaching the gate is caller error to
surface, not to paper over.

### 4. Arbitrary video ids → accepted iff exactly the canonical shape

A bare input matching `^[A-Za-z0-9_-]{11}$` passes through unchanged — that
is what makes canonicalization idempotent (Invariant 3:
`canon(canon(x)) == canon(x)`). An id embedded in any accepted URL form is
held to the same shape (`_validate`); extraction that yields anything else
is `bad_url`.

- **Wrong shape is rejected**: 10- or 12-char strings, the empty string,
  unicode/fullwidth lookalikes (`dQw4w9WgXcΩ`, `ｄＱｗ４ｗ９ＷｇＸｃＱ`),
  path-traversal payloads (`../../etc/passwd`), URL metacharacters
  (`?`, `#`, `/`, `&`, `@`), and embedded whitespace/CRLF.
- **No existence probe**: a shape-valid id is accepted without any network
  check. Whether the video exists / is embeddable / has captions is
  classified later by the fetch error taxonomy (`unavailable`, `private`,
  …). Shape can be proven offline; existence cannot — and the whole point
  of the ordering is that nothing dials YouTube until the input is proven
  safe.
- **`get_transcript_job(video_id)` never re-enters this surface**: its id is
  an opaque registry/cache key. A fabricated key simply misses → `not_found`
  (`ytt/server.py`); it is never embedded in a URL and never reaches yt-dlp.

## Deliberately accepted-but-ignored

| Input feature | Behavior | Why it is safe to ignore |
|---|---|---|
| `user:pass@` userinfo | stripped before the host check | never dialed; output contains only the id |
| `:port` | dropped by the host parser | idem — the port of the input is meaningless to a URL built from the id |
| uppercase host / scheme (`HTTPS://YOUTUBE.COM/…`) | host lowercased before the check | hosts are case-insensitive by RFC; path/query stay case-sensitive (`/WATCH` or `V=` do not match and are rejected) |
| outer whitespace | stripped once | copy-paste padding is not a security property |
| extra query params (`list=`, `t=`, `pp=`, …) | dropped | the output is the bare id; nothing else survives |
| `#fragment` | ignored by the URL parse | never used |

## Error contract

- `error_code` is the stable `"bad_url"` (`ytt.errors.BAD_URL`); the message
  names the reason and quotes the rejected input verbatim so the calling
  model can correct itself. Tool responses are authenticated-caller content;
  public surfaces (`/ytt/health`, `/ytt/metrics`) never carry them — pinned
  by `tests/unit/test_public_observability_data_leak.py`.
- Rejection is total and side-effect free (see the gate's position, above):
  the response carries `video_id: ""` — no downstream key is minted from
  unvalidated input.
- The gate has no bypass: there is no "raw URL" flag, no second entry point
  that skips canonicalize, and no configuration that widens the host set.
