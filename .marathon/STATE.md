# ytt marathon — progress journal

Append one short entry per iteration: what you did, what's next. The next iteration reads this first.

## Status: Phase 2 COMPLETE · Next: Phase 3 — concurrency.py (bounded fetch pool, bounded queue→429, per-video single-flight + failed-Future cleanup)

- Spec: `docs/plan/plan.md` (authoritative; do not edit).
- Env: `uv` at `~/.local/bin/uv`; Python 3.12.13 pinned. Always: `export PATH="$HOME/.local/bin:$PATH"` then `uv run pytest -m "not integration" -q`.
- Suite: **269 unit tests green** (`uv run pytest -m "not integration"` → "269 passed, 1 deselected").
- Key deps (uv.lock): **fastmcp 3.4.2** (≥2.11.1 ✓; 3.x major — OAuth API differs from 2.x, verify in Phase 5), **yt-dlp 2025.5.22** (pinned), starlette 1.3.1, uvicorn 0.49.0, pydantic 2.x, httpx, prometheus-client, structlog 26.1.0, hypothesis (dev).

## Implemented so far
- **Phase 0 scaffold** (`b7c5e1d`): pyproject+uv.lock, `.python-version`, Dockerfile (multi-stage, ffmpeg, pytest gate, non-root, `CMD ["ytt","serve"]`), `.dockerignore`, all `ytt/` modules as importable stubs, `tests/{unit,integration,fixtures}`, unit gate.
- **`ytt/cli.py`** — working `serve`/`test`/`selftest` argparse (`serve`→server.serve(), `test`→pytest+JSON summary, `selftest`→selftest.run_selftest()). `--help`/`--version` exit 0.
- **`ytt/errors.py`** — full `error_code` constant set + `YttError(error_code, message)` exception base.
- **`ytt/config.py`** (`44884b9`) — pydantic-settings `Settings`, full Configuration table (exact names/defaults), `parse_size` (2Gi/500Mi…), `join_path`/`route()` (path-prefix), `public_url` audience normalization, `allowed_subjects_set`, Invariant-7 validator, `validate_storage()` (PVC statvfs / emptyDir warn). `get_settings()` lru_cached.
- **`ytt/canonicalize.py`** (`f36a1ba`) — `canonicalize(url)->11-char id`, all forms, reject set→bad_url, Invariant 3 (+Hypothesis).
- **`ytt/models.py`** (`438e31c`) — TranscriptRequest/Result, Segment, WhisperJob, EgressReport; pinned Status/Mode/Source enums; query⊥start/end; exact `transcript_quality` strings + `transcript_quality_for()`.

## NEXT UNIT → Phase 3: `ytt/concurrency.py` (bounded fetch pool, single-flight, bounded queue→429)

## concurrency.py plan (Phase 3)

`ytt/concurrency.py` implements the concurrency layer (plan §Concurrency):
- `Semaphore(YTT_MAX_CONCURRENT_FETCHES)` — bounded fetch pool; overflow→bounded queue→429
- Per-video single-flight: `asyncio.Future` registry keyed by `video_id`; concurrent requests share one in-flight fetch
- After discovery, caption work branches per `(video_id, lang)` so different language requests don't dedupe
- Failed Futures removed atomically (Invariant 6: errors never wedge retries)
- WhisperJob `Semaphore(YTT_MAX_CONCURRENT_WHISPER)` — separate small pool
- `single_flight(key, coro_factory) → result` helper
- Bounded queue (asyncio.Queue with maxsize) returns 429 when full

**Tests:** unit tests in `tests/unit/test_concurrency.py` covering:
- Semaphore limits concurrent fetches
- Single-flight: N concurrent requests for same video_id trigger exactly 1 actual call
- Single-flight failed Future is removed (retry is not wedged)
- Overflow queue → 429
- Invariant 2 (property-based): concurrent requests for one video_id produce ≤1 in-flight fetch

Note: tests must mock `yt_dlp.YoutubeDL` — never hit real YouTube.

## Deferred (not doable on this box)
- `ytt-build` Argo WorkflowTemplate + Sensor → produce as files under `deploy/` in the deploy-artifacts phase. Actual docker build / argo lint are human/CI-gated.

## Log
- Iter 1: uv+py3.12 install; Phase 0 scaffold (pyproject/lock/Dockerfile/stubs/CLI/tests). 20 tests. `b7c5e1d`.
- Iter 1 (cont): config.py (`44884b9`, +32 tests), canonicalize.py + YttError (`f36a1ba`, +61 tests), models.py (`438e31c`, +10 tests). 123 unit tests green. Next: server.py skeleton.
- Iter 2: server.py skeleton (`66e291d`): FastMCP app (name/version/instructions), 2 tool stubs (get_youtube_transcript + get_transcript_job) with full plan-spec descriptions, /ytt/health custom route (unauth liveness), build_asgi_app() mounts under path_prefix, serve() → uvicorn(1 worker). 12 new unit tests (tools list, stubs, health endpoint). **135 unit tests green**. Phase 1 COMPLETE. Next: Phase 2 parse_json3.py.
- Iter 3: parse_json3.py (`ba9dcef`): rolling-caption dedup algorithm (primary window-coverage + secondary prefix-check), aAppend/pAppend spacing, manual track straight concat, tests/fixtures/rolling_asr.json + manual_track.json with pre-verified reference strings. 45 new unit tests. **180 unit tests green**. Next: fetch.py.
- Iter 4: fetch.py (`91d9207`): YDL_BASE_OPTS (no-cookies enforcement, player_client override), SEED_MAP + classify_ydl_error (13 seeds→codes, EMPTY_BODY fallback), get_available_langs, _normalize_lang_key, _find_json3_url, _select_track (manual[lang]>auto[lang]>manual[default]>auto[default]>any; lang=None: default/original>English>any), FetchResult dataclass, _do_fetch (sync core via to_thread), fetch_transcript (async: wait_for+timeout→RATE_LIMITED; ip_blocked+proxy_url→retry). 89 new unit tests. **269 unit tests green**. Next: concurrency.py.
