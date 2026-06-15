# yt-dlp Caption/Transcript Extraction (Embedded Python Library) — 2026 Mechanics

**Status:** Research note for `ytt` (Python remote MCP server that returns YouTube transcripts).
**Date:** 2026-06-14.
**Decision context:** yt-dlp chosen as the extraction base (over JDownloader). This documents the concrete in-process implementation knowledge.

---

## Summary

- yt-dlp is consumed as a library via `yt_dlp.YoutubeDL`. For transcripts you set `skip_download=True`, `writesubtitles=True`, `writeautomaticsub=True`, `subtitleslangs=[...]`, and call **`extract_info(url, download=False)`** — this returns an info dict with two relevant keys: **`subtitles`** (human/manually-authored captions) and **`automatic_captions`** (ASR / auto-generated). Each is `{lang: [ {ext, url, name}, ... ]}`.
- **Do not write files.** Instead read the per-track `url` from the info dict and fetch it yourself (via yt-dlp's own `urlopen` so it shares cookies/proxy/PO-token state). Request the **`json3`** track — it carries word/segment-level timestamps (`events[].tStartMs`, `dDurationMs`, `segs[].utf8`). Writing json3 to disk trips a 2026 `_UnsafeExtensionError` bug; fetching the URL in-process **sidesteps that entirely**.
- **Prefer manual then fall back to ASR:** check `subtitles` first, fall back to `automatic_captions`. Auto-translation appears as synthetic language tracks in `automatic_captions` (e.g. `en-orig` plus translated `fr`, `de` …).
- **PoToken (2026):** YouTube's `timedtext` (subtitle) API now requires a **`subs`-context PO token for the `web` client**. The pragmatic answer is **don't use `web` for subtitles** — use a client whose subtitle/player path doesn't require a PO token (`tv`, `web_embedded`, `mweb` for GVS-only, `android_vr`). Without a PO-token provider on a flagged/datacenter IP you'll hit empty subtitle responses ("Did not get any data blocks") and/or the bot wall.
- **IP-block surface:** wrap calls and catch `yt_dlp.utils.DownloadError` / `ExtractorError`; match on `"Sign in to confirm you're not a bot"`, `HTTP Error 429`, `HTTP Error 403`. These are the signals to rotate proxy / back off.
- **Proxy:** one option — `ydl_opts['proxy'] = 'http://user:pass@host:port'` (or `socks5://…`). Rotation is per-`YoutubeDL`-instance or via the `Ytdl-Request-Proxy` header; yt-dlp does **not** auto-rotate.
- **License:** **The Unlicense (public domain)** — embedding in a commercial/MCP service is unrestricted.

---

## 1. Subtitles via the Python API (not CLI)

The CLI flags map 1:1 to `YoutubeDL` constructor option keys:

| CLI flag | Python option key | Meaning |
|---|---|---|
| `--skip-download` | `skip_download` | Do not download media; still resolve subtitle metadata/URLs |
| `--write-subs` | `writesubtitles` | Enable manual (human) subtitle tracks |
| `--write-auto-subs` | `writeautomaticsub` | Enable auto-generated (ASR) tracks |
| `--sub-langs` | `subtitleslangs` | List/regex of languages, e.g. `['en.*','ja']`, or `['all']` |
| `--sub-format` | `subtitlesformat` | Preference string, e.g. `'json3/srv1/vtt'` |
| `--list-subs` | `listsubtitles` | Just enumerate available tracks |

### How the data comes back

`extract_info(url, download=False)` returns an info dict containing two dicts:

- `info['subtitles']` — manually authored / uploaded captions.
- `info['automatic_captions']` — ASR auto-generated captions (and auto-translations).

Each maps a language code to a list of available track formats:

```python
info['subtitles']['en'] == [
    {'ext': 'json3', 'url': 'https://www.youtube.com/api/timedtext?...&fmt=json3', 'name': 'English'},
    {'ext': 'srv1',  'url': '...'},
    {'ext': 'vtt',   'url': '...'},
    ...
]
```

### Getting text + timestamps in-process (no files on disk)

You have two choices:

1. **Let yt-dlp write the file** (`writesubtitles=True` + an `outtmpl`) then read it back. **Avoid** — as of 2026 the `json3`/`srv1`/`srv2`/`srv3`/`ttml` writers can raise `_UnsafeExtensionError` ("The extracted extension ('en.json3') is unusual and will be skipped for safety reasons", issue #10360). VTT/SRT write cleanly but VTT is lossier for word-level timing.
2. **Read the track `url` from the info dict and fetch it yourself.** Recommended. Bypasses the unsafe-extension writer bug, keeps everything in memory, and reuses yt-dlp's networking (cookies, proxy, headers, PO-token, impersonation). Fetch via `ydl.urlopen(url)` so the request inherits the same session.

### json3 structure (what you parse)

The `json3` (a.k.a. `fmt=json3`) timedtext payload is JSON shaped like:

```json
{
  "events": [
    {
      "tStartMs": 1234,
      "dDurationMs": 2500,
      "segs": [ { "utf8": "hello " }, { "utf8": "world" } ]
    },
    ...
  ]
}
```

- `tStartMs` — segment start in milliseconds.
- `dDurationMs` — duration in milliseconds.
- `segs[].utf8` — text fragments to concatenate (auto-captions emit word-level `segs`; join them).
- Some events are formatting/append-only and have no `segs` — skip events lacking `segs`.

`srv1` is a simpler XML (`<text start="1.23" dur="2.5">…</text>`) if you want a lighter parse, but `json3` is the richest for word timing.

---

## 2. Auto-generated vs manual captions

- **Distinction is structural, not a flag you parse:** manual tracks live under `info['subtitles']`; ASR tracks live under `info['automatic_captions']`. yt-dlp populates them from different parts of YouTube's player response.
- **Prefer manual, fall back to ASR:** check `info['subtitles']` for your language first; only if empty, consult `info['automatic_captions']`. (This is the long-standing user request from issue #9371 — there is no single "prefer manual" CLI switch, you implement the preference in code, which is natural in the library.)
- **Languages:** the keys of each dict are BCP-47-ish codes (`en`, `en-US`, `es`, `ja`, …). The original ASR language often appears as `<lang>-orig` (e.g. `en-orig`).
- **Auto-translation:** YouTube exposes machine-translated variants of the ASR track. These surface as **additional language keys inside `automatic_captions`** (e.g. an English ASR video will list `fr`, `de`, `es` … translated tracks). They are not "real" captions — flag them as translated/derived if your MCP surfaces provenance. Using `subtitleslangs=['all']` will enumerate them; narrow to a regex (`['en.*']`) to avoid pulling hundreds of translations.

---

## 3. PoToken in 2026

**Core fact:** YouTube's `timedtext` (subtitle) endpoint now enforces a PO token in the **`subs` context for the `web` client**. yt-dlp's subtitle request to that API returns an empty body when the token is absent ("Did not get any data blocks"). (issue #13075.)

**PO-token contexts** (the three enforcement buckets yt-dlp tracks):
- **GVS** — Google Video Server (media stream) requests.
- **Player** — Innertube player requests.
- **Subs** — subtitle (`timedtext`) requests.

**Per-client enforcement (from the official PO Token Guide, edited 2026-03-10):**

| Client | PO token needed | Cookies | Notes |
|---|---|---|---|
| `web` | **Subs + GVS** | yes | SABR-restricted; **this is the one that breaks subtitles without a provider** |
| `web_safari` | GVS | yes | HLS exempt |
| `mweb` | GVS only | yes | **datacenter-friendly**; no Subs/Player token → subtitles work |
| `tv` / `tv_simply` | GVS (tv: none for player/subs) | tv yes | `tv` listed as needing **None** for player; good fallback |
| `web_embedded` | **None** | yes | no PO token required |
| `android_vr` | **None** | — | no PO token required |
| `android` / `ios` | GVS or Player | no cookies | mobile clients |
| `web_creator` | requires sign-in every video (2026) | yes | avoid for anonymous use |

**Practical guidance for `ytt` on a server/datacenter IP:**
- For **transcripts specifically**, pick a `player_client` whose **Subs** path is not PO-gated — `tv`, `web_embedded`, `mweb`, or `android_vr`. Avoid forcing `web` for captions.
- Set it via extractor args:
  ```python
  ydl_opts['extractor_args'] = {'youtube': {'player_client': ['tv', 'web_embedded', 'mweb']}}
  ```
- yt-dlp's **`default`** player_client selection rotates a set of clients automatically; explicitly pinning the non-PO-gated clients makes caption extraction deterministic.

**The plugin (`bgutil-ytdlp-pot-provider`):**
- It is the de-facto PO-token provider, built on coletdjnz's GetPOT framework. Current version **1.3.1** (released 2026-03-07).
- Supports the **gvs, player, and subs** token contexts.
- Two deployment shapes:
  - **HTTP server (recommended):** `docker run --name bgutil-provider -d --init -p 4416:4416 brainicism/bgutil-ytdlp-pot-provider` — yt-dlp's plugin talks to it on port `4416`.
  - **Generation script:** spawned per-request (higher overhead).
- Install the yt-dlp-side plugin: `python3 -m pip install -U bgutil-ytdlp-pot-provider`. yt-dlp auto-detects it; confirm in `-v` debug output.
- **2026 caution (from the project + issue #14307 thread):** providing a PO token **no longer reliably bypasses the bot check** for most flagged IPs — "Providing a PO token does not guarantee bypassing 403 errors or bot checks." Tokens are now **bound to the video ID**, so a fresh token is needed per video.

**What breaks without a provider:** if you force or fall through to the `web` client, subtitle (`timedtext`) requests come back empty ("Did not get any data blocks"); media (GVS) requests may 403; and on a flagged IP the player request raises the bot wall. **Captions can still work without any provider** if you stick to non-`web`, non-PO-gated clients (`tv`/`web_embedded`/`mweb`/`android_vr`) **and** the IP is not bot-flagged.

---

## 4. IP-block / rate-limit error surface

All extraction errors surface as Python exceptions from `yt_dlp.utils`:

- **`yt_dlp.utils.DownloadError`** — top-level wrapper raised out of `extract_info`/`download`. Its message string carries the underlying cause.
- **`yt_dlp.utils.ExtractorError`** — raised inside extractors (often re-wrapped as `DownloadError`).
- Networking errors may surface as `yt_dlp.networking.exceptions.HTTPError` (has `.status`).

**Strings to match for fallback logic** (these are what the MCP should detect):

| Signal substring | Meaning | Action |
|---|---|---|
| `Sign in to confirm you're not a bot` | IP bot-flagged | rotate proxy / add cookies / back off |
| `HTTP Error 429` / `Too Many Requests` | rate limited | back off + rotate |
| `HTTP Error 403` / `Forbidden` | blocked / PO-token / SABR | switch client / provider |
| `Did not get any data blocks` | subtitle PO-token gate (web client) | switch player_client off `web` |
| `Video unavailable` / `Private video` | content, not IP | do not retry/rotate |

Pattern:

```python
from yt_dlp.utils import DownloadError, ExtractorError

try:
    info = ydl.extract_info(url, download=False)
except (DownloadError, ExtractorError) as e:
    msg = str(e)
    if "not a bot" in msg or "429" in msg or "Did not get any data blocks" in msg:
        # signal: rotate proxy / retry on a fresh IP
        ...
    else:
        raise
```

(There is no dedicated `IPBlockedError` class — string matching on `DownloadError.msg` is the supported approach. See issues #10890, #7143, #12264.)

---

## 5. Proxy support (Python API)

Single option key — no extra library:

```python
ydl_opts = {
    'proxy': 'http://user:pass@residential-endpoint:60000',  # or socks5://user:pass@host:1080
}
```

- Supports `http://`, `https://`, `socks4://`, `socks5://`, `socks5h://`.
- `'proxy': ''` (empty string) forces a **direct** connection (useful to override env `*_PROXY`).
- `geo_verification_proxy` is a separate key used only for geo IP verification while the main `proxy` does the work.

**Rotation:** yt-dlp does **not** round-robin automatically. Options for a rotating-residential setup:
1. New `YoutubeDL(ydl_opts)` instance per request with a freshly chosen `proxy` (simplest for an MCP that handles one video per call).
2. Per-request override via the `Ytdl-Request-Proxy` HTTP header on a custom request.
3. Point `proxy` at a rotating-gateway endpoint (the proxy provider rotates server-side); this is usually the cleanest — the endpoint stays constant, the exit IP rotates per connection.

For `ytt`, option 1 or 3 is recommended: a rotating residential gateway URL in `proxy`, and on a bot/429 signal, retry on a new `YoutubeDL` instance (forcing a fresh upstream connection / IP).

---

## 6. Cookies

| Option key | Value | Use |
|---|---|---|
| `cookiefile` | path to Netscape-format cookies.txt | static cookie jar |
| `cookiesfrombrowser` | tuple, e.g. `('chrome',)` or `('firefox', profile, keyring, container)` | pull live from a local browser |

**When needed:**
- **Anonymous transcript extraction usually does NOT need cookies** if you use a non-PO-gated client and a clean IP. Captions are public.
- Cookies become relevant when the IP is bot-flagged ("Sign in to confirm you're not a bot") or for age-restricted/members-only content.

**Tradeoffs / risks of account cookies server-side (important for an MCP):**
- Account cookies tie all traffic to a real Google account; heavy automated use risks **account suspension/lock**, not just an IP block.
- yt-dlp's own docs/community warn that **rotating cookies from a logged-in account through datacenter IPs is the fastest way to get the account flagged**. Prefer **cookies from a throwaway/secondary account**, or no cookies + clean residential IP.
- `cookiesfrombrowser` requires a real browser profile on the host — awkward in a containerized MCP; `cookiefile` is the deployable form (mount a cookies.txt).
- Cookies expire; you need a refresh story if you rely on them.

**Recommendation for `ytt`:** default to **no cookies**, non-`web` player client, residential rotating proxy. Treat cookies as an optional, per-deployment escalation for bot-flagged scenarios — and if used, a dedicated burner account only.

---

## 7. License

yt-dlp is released under **The Unlicense** — public domain ("This is free and unencumbered software released into the public domain. Anyone is free to copy, modify, publish, use, compile, sell, or distribute this software … for any purpose, commercial or otherwise"). **Embedding yt-dlp as a library inside the `ytt` MCP service — including a commercial/hosted service — is fully permitted with no attribution or copyleft obligations.** (Note this is about the *software* license; you remain responsible for complying with YouTube's ToS / applicable law in how you operate the service.)

---

## Ready-to-adapt snippet: fetch transcript segments for a video ID

```python
import json
import yt_dlp
from yt_dlp.utils import DownloadError, ExtractorError


class TranscriptUnavailable(Exception):
    pass


class IPBlocked(Exception):
    """Raise to the caller so the MCP can rotate proxy / retry."""


def fetch_transcript_segments(video_id: str, *, langs=("en", "en-US", "en-orig"),
                              proxy: str | None = None):
    """
    Return (segments, meta).
    segments: list[{"start": float, "dur": float, "text": str}]
    meta:     {"lang": str, "kind": "manual" | "auto"}
    Raises TranscriptUnavailable or IPBlocked.
    """
    url = f"https://www.youtube.com/watch?v={video_id}"

    ydl_opts = {
        "skip_download": True,          # never touch media
        "writesubtitles": True,         # enable manual tracks
        "writeautomaticsub": True,      # enable ASR tracks
        "subtitleslangs": list(langs),  # narrow language set
        "subtitlesformat": "json3",     # word/segment timing
        "quiet": True,
        "no_warnings": True,
        # Pick clients whose subtitle path is NOT PO-token-gated (avoid `web`):
        "extractor_args": {"youtube": {"player_client": ["tv", "web_embedded", "mweb"]}},
    }
    if proxy:
        ydl_opts["proxy"] = proxy

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

            manual = info.get("subtitles") or {}
            auto = info.get("automatic_captions") or {}

            # Prefer manual, then ASR. First language match wins.
            track, lang, kind = None, None, None
            for source, k in ((manual, "manual"), (auto, "auto")):
                for want in langs:
                    if want in source:
                        track, lang, kind = source[want], want, k
                        break
                if track:
                    break

            if not track:
                raise TranscriptUnavailable(f"no caption track for {langs} on {video_id}")

            # Choose the json3 entry (fall back to first available).
            entry = next((t for t in track if t.get("ext") == "json3"), track[0])

            # Fetch the timedtext URL through yt-dlp's own session
            # (shares cookies/proxy/headers/PO-token state).
            raw = ydl.urlopen(entry["url"]).read().decode("utf-8")

        if not raw.strip():
            # Empty body == "Did not get any data blocks": PO-token / web-client gate.
            raise IPBlocked(f"empty timedtext for {video_id} (PO-token/subs gate)")

        data = json.loads(raw)
        segments = []
        for ev in data.get("events", []):
            segs = ev.get("segs")
            if not segs:
                continue
            text = "".join(s.get("utf8", "") for s in segs).strip()
            if not text:
                continue
            segments.append({
                "start": ev.get("tStartMs", 0) / 1000.0,
                "dur": ev.get("dDurationMs", 0) / 1000.0,
                "text": text,
            })

        if not segments:
            raise TranscriptUnavailable(f"empty transcript for {video_id}")

        return segments, {"lang": lang, "kind": kind}

    except (DownloadError, ExtractorError) as e:
        msg = str(e)
        if any(s in msg for s in (
            "not a bot", "HTTP Error 429", "Too Many Requests",
            "Did not get any data blocks", "HTTP Error 403",
        )):
            raise IPBlocked(msg) from e
        raise TranscriptUnavailable(msg) from e
```

**Notes for the MCP layer:**
- Catch `IPBlocked` → rotate the residential proxy / retry on a fresh `YoutubeDL` instance; catch `TranscriptUnavailable` → return a clean "no transcript" to the client (don't retry).
- Reading `entry["url"]` in-process avoids the `_UnsafeExtensionError` json3-writer bug.
- To enumerate every available language first (e.g. for a "list languages" MCP tool), run with `subtitleslangs=['all']` (or `listsubtitles=True`) and read the keys of `subtitles` / `automatic_captions`.

---

## Sources

- [yt-dlp README (master) — options reference](https://raw.githubusercontent.com/yt-dlp/yt-dlp/master/README.md)
- [yt-dlp LICENSE (The Unlicense)](https://raw.githubusercontent.com/yt-dlp/yt-dlp/master/LICENSE)
- [yt-dlp PO Token Guide (wiki, edited 2026-03-10)](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide)
- [yt-dlp YouTube `_base.py` INNERTUBE_CLIENTS (source)](https://raw.githubusercontent.com/yt-dlp/yt-dlp/master/yt_dlp/extractor/youtube/_base.py)
- [Issue #13075 — "[youtube] Some subtitles require POT now?"](https://github.com/yt-dlp/yt-dlp/issues/13075)
- [Issue #14307 — "[youtube] question about player PO tokens with the web client"](https://github.com/yt-dlp/yt-dlp/issues/14307)
- [Issue #10360 — `_UnsafeExtensionError` on json3/srv1/srv2/srv3 sub-format](https://github.com/yt-dlp/yt-dlp/issues/10360)
- [Issue #9371 — preferentially download manual over auto subtitles](https://github.com/yt-dlp/yt-dlp/issues/9371)
- [Issue #10890 — Error 429 / requires sign-in](https://github.com/yt-dlp/yt-dlp/issues/10890)
- [Issue #7143 — Skipping player response / HTTP 429 / Video Not Available](https://github.com/yt-dlp/yt-dlp/issues/7143)
- [Issue #12264 — solving "not a bot" via cookies/proxies](https://github.com/yt-dlp/yt-dlp/issues/12264)
- [Brainicism/bgutil-ytdlp-pot-provider (v1.3.1, 2026-03-07)](https://github.com/Brainicism/bgutil-ytdlp-pot-provider)
- [bgutil-ytdlp-pot-provider on PyPI](https://pypi.org/project/bgutil-ytdlp-pot-provider)
- [Oxylabs — yt_dlp proxy integration (Python API)](https://developers.oxylabs.io/video-data/high-bandwidth-proxies/youtube-downloader-yt_dlp-integration)
- [Instagit — yt-dlp networking: HTTP, impersonation, proxy rotation](https://instagit.com/yt-dlp/yt-dlp/yt-dlp-networking-layer-http-requests-impersonation-proxy-rotation/)
- [SkipTheWatch — Extract YouTube Subtitles with yt-dlp (2026)](https://skipthewatch.com/blog/yt-dlp-youtube-subtitles)
