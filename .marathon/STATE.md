# ytt marathon — progress journal

Append one short entry per iteration: what you did, what's next. The next iteration reads this first.

## Status: Phase 4 COMPLETE · Next: Phase 5 — auth.py + authz.py (FastMCP OAuth audience-bound, subject allowlist, per-subject rate limit, OAuth metadata routes)

- Spec: `docs/plan/plan.md` (authoritative; do not edit).
- Env: `uv` at `~/.local/bin/uv`; Python 3.12.13 pinned. Always: `export PATH="$HOME/.local/bin:$PATH"` then `uv run pytest -m "not integration" -q`.
- Suite: **329 unit tests green** (`uv run pytest -m "not integration"` → "329 passed, 1 deselected").
- Key deps (uv.lock): **fastmcp 3.4.2** (≥2.11.1 ✓; 3.x major — OAuth API differs from 2.x, verify in Phase 5), **yt-dlp 2025.5.22** (pinned), starlette 1.3.1, uvicorn 0.49.0, pydantic 2.x, httpx, prometheus-client, structlog 26.1.0, hypothesis (dev).

## Implemented so far
- **Phase 0 scaffold** (`b7c5e1d`): pyproject+uv.lock, `.python-version`, Dockerfile (multi-stage, ffmpeg, pytest gate, non-root, `CMD ["ytt","serve"]`), `.dockerignore`, all `ytt/` modules as importable stubs, `tests/{unit,integration,fixtures}`, unit gate.
- **`ytt/cli.py`** — working `serve`/`test`/`selftest` argparse (`serve`→server.serve(), `test`→pytest+JSON summary, `selftest`→selftest.run_selftest()). `--help`/`--version` exit 0.
- **`ytt/errors.py`** — full `error_code` constant set + `YttError(error_code, message)` exception base.
- **`ytt/config.py`** (`44884b9`) — pydantic-settings `Settings`, full Configuration table (exact names/defaults), `parse_size` (2Gi/500Mi…), `join_path`/`route()` (path-prefix), `public_url` audience normalization, `allowed_subjects_set`, Invariant-7 validator, `validate_storage()` (PVC statvfs / emptyDir warn). `get_settings()` lru_cached.
- **`ytt/canonicalize.py`** (`f36a1ba`) — `canonicalize(url)->11-char id`, all forms, reject set→bad_url, Invariant 3 (+Hypothesis).
- **`ytt/models.py`** (`438e31c`) — TranscriptRequest/Result, Segment, WhisperJob, EgressReport; pinned Status/Mode/Source enums; query⊥start/end; exact `transcript_quality` strings + `transcript_quality_for()`.

## NEXT UNIT → Phase 5: `ytt/auth.py` + `ytt/authz.py` + OAuth metadata routes

### Phase 5 plan

**auth.py**: FastMCP OAuth setup, audience-bound to `https://mcp.ardenone.com/ytt`. DCR off. Key spike: FastMCP 3.x OAuth API (verify how to set `resource`/`issuer`/`audience`; 3.x differs from 2.x docs). If FastMCP can't emit path-inserted/path-bearing RFC 9728 metadata natively, add custom Starlette routes.

**authz.py**: subject allowlist check (`sub` claim ∈ `YTT_ALLOWED_SUBJECTS` → 403 with human-readable message). `selftest --show-sub` mechanism (write /tmp/ytt_last_sub on first successful auth). Apply allowlist at tool-call time (not at /authorize).

**ratelimit.py**: Per-subject token bucket (`YTT_RATE_LIMIT_PER_MIN`); queue-full → 429+Retry-After. Also Whisper quota (`YTT_WHISPER_JOBS_PER_HOUR`). Stub for now (no FastMCP hook yet).

**OAuth metadata** custom Starlette routes (if FastMCP doesn't emit them):
- `/.well-known/oauth-protected-resource/ytt` — RFC 9728 PRM doc (`resource=https://mcp.ardenone.com/ytt`)
- `/.well-known/oauth-authorization-server/ytt` — AS metadata
- `WWW-Authenticate` header: `Bearer resource_metadata="…"` on 401

Tests: 401 on unauthenticated, 403 on non-allowlisted sub, allowlist allow/deny, rate-limit bucket refill + per-subject isolation, wrong-audience token rejected, OAuth metadata shape.

## Deferred (not doable on this box)
- `ytt-build` Argo WorkflowTemplate + Sensor → produce as files under `deploy/` in the deploy-artifacts phase. Actual docker build / argo lint are human/CI-gated.

## Log
- Iter 1: uv+py3.12 install; Phase 0 scaffold (pyproject/lock/Dockerfile/stubs/CLI/tests). 20 tests. `b7c5e1d`.
- Iter 1 (cont): config.py (`44884b9`, +32 tests), canonicalize.py + YttError (`f36a1ba`, +61 tests), models.py (`438e31c`, +10 tests). 123 unit tests green. Next: server.py skeleton.
- Iter 2: server.py skeleton (`66e291d`): FastMCP app (name/version/instructions), 2 tool stubs (get_youtube_transcript + get_transcript_job) with full plan-spec descriptions, /ytt/health custom route (unauth liveness), build_asgi_app() mounts under path_prefix, serve() → uvicorn(1 worker). 12 new unit tests (tools list, stubs, health endpoint). **135 unit tests green**. Phase 1 COMPLETE. Next: Phase 2 parse_json3.py.
- Iter 3: parse_json3.py (`ba9dcef`): rolling-caption dedup algorithm (primary window-coverage + secondary prefix-check), aAppend/pAppend spacing, manual track straight concat, tests/fixtures/rolling_asr.json + manual_track.json with pre-verified reference strings. 45 new unit tests. **180 unit tests green**. Next: fetch.py.
- Iter 4: fetch.py (`91d9207`): YDL_BASE_OPTS (no-cookies enforcement, player_client override), SEED_MAP + classify_ydl_error (13 seeds→codes, EMPTY_BODY fallback), get_available_langs, _normalize_lang_key, _find_json3_url, _select_track (manual[lang]>auto[lang]>manual[default]>auto[default]>any; lang=None: default/original>English>any), FetchResult dataclass, _do_fetch (sync core via to_thread), fetch_transcript (async: wait_for+timeout→RATE_LIMITED; ip_blocked+proxy_url→retry). 89 new unit tests. **269 unit tests green**. Next: concurrency.py.
- Iter 5: concurrency.py (`538cf71`): SingleFlightRegistry (Future dedup by key, Invariant 2+6, shield-based waiter sharing, key removed on success+failure, remove() for forced eviction), BoundedFetchPool (Semaphore + counter-tracked soft queue, 429 when active+qd>=max_concurrent+max_queue, cancellation-safe finally), ConcurrencyState (container with from_settings). 26 new tests (including Hypothesis property-based Invariant 2 + bounded-pool depth invariant). **295 unit tests green**. Next: cache.py.
- Iter 6: cache.py (`7622b10`): TranscriptCache (startup_scan, get/put/reconcile/evict_lru/shutdown), CacheHit dataclass, flat-file LRU (txt+json unit pairs), byte-cap Invariant 1 (asyncio lock discipline), ENOSPC degrade, whisper fallback, reconcile drift correction, 34 new unit tests (Hypothesis Invariant 1 property test included). **329 unit tests green**. Next: Phase 5 auth.py+authz.py.
