# ytt marathon — progress journal

Append one short entry per iteration: what you did, what's next. The next iteration reads this first.

## Status: Phase 1 COMPLETE · Phase 2 next (fetch core: parse_json3 dedup + fixtures + fetch.py + error taxonomy)

- Spec: `docs/plan/plan.md` (authoritative; do not edit).
- Env: `uv` at `~/.local/bin/uv`; Python 3.12.13 pinned. Always: `export PATH="$HOME/.local/bin:$PATH"` then `uv run pytest -m "not integration" -q`.
- Suite: **123 unit tests green** (`uv run pytest -m "not integration"` → "123 passed, 1 deselected").
- Key deps (uv.lock): **fastmcp 3.4.2** (≥2.11.1 ✓; 3.x major — OAuth API differs from 2.x, verify in Phase 5), **yt-dlp 2025.5.22** (pinned), starlette 1.3.1, uvicorn 0.49.0, pydantic 2.x, httpx, prometheus-client, structlog 26.1.0, hypothesis (dev).

## Implemented so far
- **Phase 0 scaffold** (`b7c5e1d`): pyproject+uv.lock, `.python-version`, Dockerfile (multi-stage, ffmpeg, pytest gate, non-root, `CMD ["ytt","serve"]`), `.dockerignore`, all `ytt/` modules as importable stubs, `tests/{unit,integration,fixtures}`, unit gate.
- **`ytt/cli.py`** — working `serve`/`test`/`selftest` argparse (`serve`→server.serve(), `test`→pytest+JSON summary, `selftest`→selftest.run_selftest()). `--help`/`--version` exit 0.
- **`ytt/errors.py`** — full `error_code` constant set + `YttError(error_code, message)` exception base.
- **`ytt/config.py`** (`44884b9`) — pydantic-settings `Settings`, full Configuration table (exact names/defaults), `parse_size` (2Gi/500Mi…), `join_path`/`route()` (path-prefix), `public_url` audience normalization, `allowed_subjects_set`, Invariant-7 validator, `validate_storage()` (PVC statvfs / emptyDir warn). `get_settings()` lru_cached.
- **`ytt/canonicalize.py`** (`f36a1ba`) — `canonicalize(url)->11-char id`, all forms, reject set→bad_url, Invariant 3 (+Hypothesis).
- **`ytt/models.py`** (`438e31c`) — TranscriptRequest/Result, Segment, WhisperJob, EgressReport; pinned Status/Mode/Source enums; query⊥start/end; exact `transcript_quality` strings + `transcript_quality_for()`.

## NEXT UNIT → Phase 2: `ytt/parse_json3.py` (rolling-caption dedup) + fixtures

Phase 2 is the fetch core. Start with `parse_json3.py` since it is flagged as the #1 silent bug:

**`ytt/parse_json3.py`** — parse a json3 caption track (dict already loaded):
- `parse_json3(events, kind="asr") -> list[Segment]` — Segment = {start_ms, duration_ms, text}.
- **DEDUP algorithm** (plan §Fetch core point 2): walk events ascending `tStartMs`; skip no-utf8 events; maintain `last_end_ms=0`; emit only if `tStartMs >= last_end_ms`; update `last_end_ms = tStartMs + dDurationMs`; collect segments respecting `aAppend`/`pAppend` spacing.
- **Prefix check** (secondary dedup): discard an event if its text is a strict prefix of the next event's text (whitespace-stripped, case-sensitive).
- Manual tracks (`kind != "asr"`) → straight concat (no dedup needed).
- Mandatory fixtures: one rolling auto track (assert no doubling, assert matches reference) + one manual track → both in `tests/fixtures/`.

**Tests:** unit tests in `tests/unit/test_parse_json3.py` covering:
- Manual track straight concat (no dedup).
- Rolling auto track → no-doubling + reference output.
- aAppend/pAppend spacing rules.
- Empty events filtered out.
- Prefix-check dedup.

After parse_json3: move to `ytt/fetch.py` (yt-dlp integration + error taxonomy + language selection + no-cookies assertion test).

## Deferred (not doable on this box)
- `ytt-build` Argo WorkflowTemplate + Sensor → produce as files under `deploy/` in the deploy-artifacts phase. Actual docker build / argo lint are human/CI-gated.

## Log
- Iter 1: uv+py3.12 install; Phase 0 scaffold (pyproject/lock/Dockerfile/stubs/CLI/tests). 20 tests. `b7c5e1d`.
- Iter 1 (cont): config.py (`44884b9`, +32 tests), canonicalize.py + YttError (`f36a1ba`, +61 tests), models.py (`438e31c`, +10 tests). 123 unit tests green. Next: server.py skeleton.
- Iter 2: server.py skeleton (`66e291d`): FastMCP app (name/version/instructions), 2 tool stubs (get_youtube_transcript + get_transcript_job) with full plan-spec descriptions, /ytt/health custom route (unauth liveness), build_asgi_app() mounts under path_prefix, serve() → uvicorn(1 worker). 12 new unit tests (tools list, stubs, health endpoint). **135 unit tests green**. Phase 1 COMPLETE. Next: Phase 2 parse_json3.py.
