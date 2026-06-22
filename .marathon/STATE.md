# ytt marathon — progress journal

Append one short entry per iteration: what you did, what's next. The next iteration reads this first.

## Status: Phase 2 parse_json3 COMPLETE · Next: fetch.py (yt-dlp integration + error taxonomy + language selection + no-cookies test)

- Spec: `docs/plan/plan.md` (authoritative; do not edit).
- Env: `uv` at `~/.local/bin/uv`; Python 3.12.13 pinned. Always: `export PATH="$HOME/.local/bin:$PATH"` then `uv run pytest -m "not integration" -q`.
- Suite: **180 unit tests green** (`uv run pytest -m "not integration"` → "180 passed, 1 deselected").
- Key deps (uv.lock): **fastmcp 3.4.2** (≥2.11.1 ✓; 3.x major — OAuth API differs from 2.x, verify in Phase 5), **yt-dlp 2025.5.22** (pinned), starlette 1.3.1, uvicorn 0.49.0, pydantic 2.x, httpx, prometheus-client, structlog 26.1.0, hypothesis (dev).

## Implemented so far
- **Phase 0 scaffold** (`b7c5e1d`): pyproject+uv.lock, `.python-version`, Dockerfile (multi-stage, ffmpeg, pytest gate, non-root, `CMD ["ytt","serve"]`), `.dockerignore`, all `ytt/` modules as importable stubs, `tests/{unit,integration,fixtures}`, unit gate.
- **`ytt/cli.py`** — working `serve`/`test`/`selftest` argparse (`serve`→server.serve(), `test`→pytest+JSON summary, `selftest`→selftest.run_selftest()). `--help`/`--version` exit 0.
- **`ytt/errors.py`** — full `error_code` constant set + `YttError(error_code, message)` exception base.
- **`ytt/config.py`** (`44884b9`) — pydantic-settings `Settings`, full Configuration table (exact names/defaults), `parse_size` (2Gi/500Mi…), `join_path`/`route()` (path-prefix), `public_url` audience normalization, `allowed_subjects_set`, Invariant-7 validator, `validate_storage()` (PVC statvfs / emptyDir warn). `get_settings()` lru_cached.
- **`ytt/canonicalize.py`** (`f36a1ba`) — `canonicalize(url)->11-char id`, all forms, reject set→bad_url, Invariant 3 (+Hypothesis).
- **`ytt/models.py`** (`438e31c`) — TranscriptRequest/Result, Segment, WhisperJob, EgressReport; pinned Status/Mode/Source enums; query⊥start/end; exact `transcript_quality` strings + `transcript_quality_for()`.

## NEXT UNIT → Phase 2 remainder: `ytt/fetch.py` (yt-dlp integration + error taxonomy + language selection)

## fetch.py plan (Phase 2 remainder)

`ytt/fetch.py` is the yt-dlp integration layer:

**`ytt/fetch.py`** — yt-dlp integration:
- `fetch_transcript(video_id, lang, settings) -> (list[Segment], metadata, source, served_lang)`
- `extract_info` call wrapped in `asyncio.to_thread` + `asyncio.wait_for(timeout=YTT_EXTRACT_TIMEOUT_SEC)`
- Error taxonomy: catch `DownloadError` / `ExtractorError`, match seed strings to error codes
- Language selection: manual[lang] → auto[lang] → manual[default] → auto[default]
- No-cookies enforcement: ydl_opts must have `cookiefile=None, cookiesfrombrowser=None`
- extractor_args: `{'youtube': {'player_client': ['tv', 'web_embedded', 'mweb']}}`
- Fetch json3 URL via `ydl.urlopen()`, pass to `parse_json3()`
- available_langs: strip `.auto` suffix, lowercase, deduplicate

**Tests:** unit tests in `tests/unit/test_fetch.py` covering:
- No-cookies assertion (assert keys present and falsy in ydl_opts)
- Error taxonomy: each seed string → correct error_code (mock DownloadError)
- Language fallback logic
- Stubbed yt-dlp extract_info (no network)
- Proxy retry on ip_blocked

Note: tests must mock `yt_dlp.YoutubeDL` — never hit real YouTube.

## Deferred (not doable on this box)
- `ytt-build` Argo WorkflowTemplate + Sensor → produce as files under `deploy/` in the deploy-artifacts phase. Actual docker build / argo lint are human/CI-gated.

## Log
- Iter 1: uv+py3.12 install; Phase 0 scaffold (pyproject/lock/Dockerfile/stubs/CLI/tests). 20 tests. `b7c5e1d`.
- Iter 1 (cont): config.py (`44884b9`, +32 tests), canonicalize.py + YttError (`f36a1ba`, +61 tests), models.py (`438e31c`, +10 tests). 123 unit tests green. Next: server.py skeleton.
- Iter 2: server.py skeleton (`66e291d`): FastMCP app (name/version/instructions), 2 tool stubs (get_youtube_transcript + get_transcript_job) with full plan-spec descriptions, /ytt/health custom route (unauth liveness), build_asgi_app() mounts under path_prefix, serve() → uvicorn(1 worker). 12 new unit tests (tools list, stubs, health endpoint). **135 unit tests green**. Phase 1 COMPLETE. Next: Phase 2 parse_json3.py.
- Iter 3: parse_json3.py (`ba9dcef`): rolling-caption dedup algorithm (primary window-coverage + secondary prefix-check), aAppend/pAppend spacing, manual track straight concat, tests/fixtures/rolling_asr.json + manual_track.json with pre-verified reference strings. 45 new unit tests. **180 unit tests green**. Next: fetch.py.
