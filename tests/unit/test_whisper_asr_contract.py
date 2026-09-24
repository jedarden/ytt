"""OpenAI-compatible ASR protocol contract tests (README §Configuration).

The README promises: "Point ``YTT_WHISPER_URL`` at any OpenAI-compatible ASR
service (``/v1/audio/transcriptions``)". This module pins the wire contract
``run_whisper_job`` actually speaks, using a fake ASR service on
``httpx.MockTransport`` — a real ``httpx.AsyncClient`` builds each request
(multipart encoding, headers, URL) exactly as it would on the network; only
the socket is replaced. The earlier ``test_whisper.py`` tests mock the client
object itself, so the request-construction layer between
``client.post(files=..., data=...)`` and the wire is not exercised there.

Coverage:
- request shape: POST to ``{whisper_url}/v1/audio/transcriptions``, body is
  ``multipart/form-data`` with a boundary
- multipart field names: ``file`` (filename = scratch audio name, part type
  ``audio/mpeg``, bytes = the downloaded audio), ``model``,
  ``response_format=verbose_json``
- model selection: the ``model`` field carries the *effective* model
  (``run_whisper_job``'s ``active_model`` — the model-guard output), not
  ``settings.whisper_model``
- successful parsing: verbose_json ``text``/``segments``/``language`` →
  cache unit (``duration = end − start``, non-dict segment entries skipped),
  job ``done`` with ``result_ref = {video_id}.whisper``
- upstream errors: 4xx/5xx → job ``error`` / ``asr_failed``, status code and
  response-body snippet in the message, nothing cached
- timeouts: ``httpx.ReadTimeout`` → job ``error`` / ``asr_failed`` with the
  timeout message. (``MockTransport`` bypasses httpcore's timers, so the fake
  surfaces the timeout by raising the same ``httpx.TimeoutException`` the
  real connection pool raises — the contract under test is the mapping.)
- malformed responses: non-JSON, empty, and non-object 200 bodies all land in
  ``error`` / ``asr_failed``; a JSON object *missing* ``text`` is tolerated —
  the job completes with an empty transcript (pinned current behavior: an
  OpenAI-compatible service always returns ``text``, and a tolerated miss
  must not wedge the FSM into an error-retry loop).
"""

from __future__ import annotations

from email.parser import BytesParser
from email.policy import HTTP
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from ytt import errors
from ytt.whisper import WhisperJobRegistry, run_whisper_job

VIDEO_ID = "dQw4w9WgXcQ"
AUDIO_BYTES = b"\x89fake-audio-payload-\xde\xad\xbe\xef"

DEFAULT_VERBOSE_JSON: dict[str, Any] = {
    "text": "Hello world",
    "language": "en",
    "segments": [{"id": 0, "start": 0.0, "end": 2.0, "text": "Hello world"}],
}


# ---------------------------------------------------------------------------
# Fake ASR service (httpx.MockTransport)
# ---------------------------------------------------------------------------


def _parse_multipart(request: httpx.Request) -> dict[str, Any]:
    """Parse a ``multipart/form-data`` body into fields and file parts.

    Returns ``{"fields": {name: str}, "files": {name: {filename, content_type,
    data}}} ``. Uses the stdlib email parser over a minimal MIME envelope
    (the body is already wire-format multipart; only the outer headers are
    missing).
    """
    body = request.content
    envelope = (
        b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n"
        b"MIME-Version: 1.0\r\n\r\n" + body
    )
    msg = BytesParser(policy=HTTP).parsebytes(envelope)

    fields: dict[str, str] = {}
    files: dict[str, dict[str, Any]] = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name is None:
            continue
        payload = part.get_payload(decode=True) or b""
        if part.get_filename() is not None:
            files[name] = {
                "filename": part.get_filename(),
                "content_type": part.get_content_type(),
                "data": payload,
            }
        else:
            fields[name] = payload.decode()
    return {"fields": fields, "files": files}


class FakeASRService:
    """In-process stand-in for an OpenAI-compatible transcription service.

    Records every request (method, path, parsed multipart) and answers with a
    programmable response. Defaults to a well-formed ``verbose_json`` 200.
    """

    def __init__(
        self,
        *,
        status_code: int = 200,
        json_body: Any = DEFAULT_VERBOSE_JSON,
        raw_body: bytes | None = None,
        content_type: str = "application/json",
        raise_exc: Exception | None = None,
    ) -> None:
        self.status_code = status_code
        self.json_body = json_body
        self.raw_body = raw_body
        self.content_type = content_type
        self.raise_exc = raise_exc
        self.raw_requests: list[httpx.Request] = []
        self.requests: list[dict[str, Any]] = []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        self.raw_requests.append(request)
        self.requests.append(
            {
                "method": request.method,
                "path": request.url.path,
                "content_type": request.headers.get("content-type", ""),
                "multipart": _parse_multipart(request),
            }
        )
        if self.raise_exc is not None:
            raise self.raise_exc
        if self.raw_body is not None:
            return httpx.Response(
                self.status_code,
                content=self.raw_body,
                headers={"content-type": self.content_type},
            )
        return httpx.Response(self.status_code, json=self.json_body)

    @property
    def last(self) -> dict[str, Any]:
        assert self.requests, "fake ASR service received no requests"
        return self.requests[-1]


def _asr_client(service: FakeASRService) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(service.handle))


# ---------------------------------------------------------------------------
# Job scaffolding (same shape as test_whisper.py)
# ---------------------------------------------------------------------------


def _make_settings(scratch_dir: str) -> MagicMock:
    s = MagicMock()
    s.max_asr_duration_sec = 1200
    s.whisper_realtime_factor = 1.2
    s.job_ttl_sec = 3600
    s.whisper_timeout_sec = 2880
    s.max_audio_bytes = 500 * 1024 * 1024
    s.scratch_dir = scratch_dir
    s.whisper_url = "http://asr.test:9000"
    # Deliberately different from the active model used in most tests: the
    # wire `model` field must follow run_whisper_job's active_model (the
    # model-guard output), never this configured name.
    s.whisper_model = "Systran/faster-whisper-small"
    s.proxy_url = None
    return s


def _make_cache() -> MagicMock:
    cache = MagicMock()
    cache.put = AsyncMock(return_value=True)
    return cache


async def _drive_job(
    registry: WhisperJobRegistry,
    settings: MagicMock,
    cache: MagicMock,
    service: FakeASRService,
    active_model: str = "tiny.en",
) -> WhisperJob:
    scratch = Path(settings.scratch_dir)
    scratch.mkdir(parents=True, exist_ok=True)
    audio_file = scratch / f"{VIDEO_ID}.mp4"
    audio_file.write_bytes(AUDIO_BYTES)

    job, _ = await registry.get_or_create(VIDEO_ID, 50.0, settings)
    with patch(
        "ytt.whisper._do_download_audio", return_value=str(audio_file)
    ):
        await run_whisper_job(
            job,
            registry,
            settings,
            cache,
            active_model,
            http_client=_asr_client(service),
        )
    final = await registry.get(VIDEO_ID)
    assert final is not None
    return final


def _make_cache() -> MagicMock:
    cache = MagicMock()
    cache.put = AsyncMock(return_value=True)
    return cache


# ---------------------------------------------------------------------------
# Request shape: method, path, multipart field names
# ---------------------------------------------------------------------------


class TestTranscriptionRequestShape:

    async def test_post_to_openai_transcriptions_path_multipart(
        self, tmp_path: Path
    ) -> None:
        service = FakeASRService()
        settings = _make_settings(str(tmp_path / "scratch"))
        await _drive_job(WhisperJobRegistry(), settings, _make_cache(), service)

        assert service.requests
        req = service.last
        assert req["method"] == "POST"
        assert req["path"] == "/v1/audio/transcriptions"
        ctype = req["content_type"]
        assert ctype.startswith("multipart/form-data")
        assert "boundary=" in ctype

    async def test_multipart_field_names_and_file_part(self, tmp_path: Path) -> None:
        service = FakeASRService()
        settings = _make_settings(str(tmp_path / "scratch"))
        await _drive_job(WhisperJobRegistry(), settings, _make_cache(), service)

        multipart = service.last["multipart"]

        # Exactly the three OpenAI fields the protocol needs — `file` (the
        # audio), `model`, `response_format`.
        assert set(multipart["fields"]) == {"model", "response_format"}
        assert multipart["fields"]["response_format"] == "verbose_json"

        # The `file` part: scratch filename, mp3 part type, exact audio bytes.
        assert set(multipart["files"]) == {"file"}
        file_part = multipart["files"]["file"]
        assert file_part["filename"] == f"{VIDEO_ID}.mp4"
        assert file_part["content_type"] == "audio/mpeg"
        assert file_part["data"] == AUDIO_BYTES

    async def test_model_field_is_effective_model_not_configured(
        self, tmp_path: Path
    ) -> None:
        """`model` on the wire = active_model (model-guard output).

        The startup guard self-corrects settings.whisper_model to a served
        model; run_whisper_job must transmit that effective name, so the fake
        service asserts the two are distinguishable here.
        """
        service = FakeASRService()
        settings = _make_settings(str(tmp_path / "scratch"))
        await _drive_job(
            WhisperJobRegistry(),
            settings,
            _make_cache(),
            service,
            active_model="tiny.en",
        )

        assert settings.whisper_model == "Systran/faster-whisper-small"
        assert service.last["multipart"]["fields"]["model"] == "tiny.en"


# ---------------------------------------------------------------------------
# Successful text parsing (verbose_json)
# ---------------------------------------------------------------------------


class TestVerboseJsonParsing:

    async def test_transcript_cached_and_job_done(self, tmp_path: Path) -> None:
        service = FakeASRService()
        settings = _make_settings(str(tmp_path / "scratch"))
        cache = _make_cache()
        registry = WhisperJobRegistry()

        final = await _drive_job(registry, settings, cache, service)

        assert final.status == "done"
        assert final.result_ref == f"{VIDEO_ID}.whisper"
        assert final.error_code is None

        cache.put.assert_called_once()
        args = cache.put.call_args[0]
        assert args[0] == VIDEO_ID  # video_id
        assert args[1] == "whisper"  # lang key — whisper satisfies any lang
        assert args[2] == "Hello world"  # text
        assert args[3] == [
            {
                "start": 0.0,
                "duration": pytest.approx(2.0),
                "text": "Hello world",
            }
        ]  # segments: {start, duration = end − start, text}
        assert args[4] == "whisper"  # source
        assert args[5] == {"detected_language": "en", "duration_sec": 50.0}

    async def test_multi_segment_durations_and_missing_language(
        self, tmp_path: Path
    ) -> None:
        service = FakeASRService(
            json_body={
                "text": "Hello world",
                "segments": [
                    {"id": 0, "start": 0.0, "end": 2.5, "text": "Hello"},
                    {"id": 1, "start": 2.5, "end": 5.0, "text": "world"},
                ],
            }
        )
        settings = _make_settings(str(tmp_path / "scratch"))
        cache = _make_cache()

        final = await _drive_job(WhisperJobRegistry(), settings, cache, service)

        assert final.status == "done"
        args = cache.put.call_args[0]
        assert args[3] == [
            {"start": 0.0, "duration": pytest.approx(2.5), "text": "Hello"},
            {"start": 2.5, "duration": pytest.approx(2.5), "text": "world"},
        ]
        # No `language` in the response → no detected_language metadata.
        assert args[5] == {"duration_sec": 50.0}

    async def test_non_dict_segment_entries_skipped(self, tmp_path: Path) -> None:
        """Tolerant parse: junk segment entries never crash the job."""
        service = FakeASRService(
            json_body={
                "text": "Hello",
                "language": "en",
                "segments": [
                    "junk-string",
                    {"start": 1.0, "end": 3.0, "text": "Hello"},
                    None,
                ],
            }
        )
        settings = _make_settings(str(tmp_path / "scratch"))
        cache = _make_cache()

        final = await _drive_job(WhisperJobRegistry(), settings, cache, service)

        assert final.status == "done"
        segments = cache.put.call_args[0][3]
        assert segments == [
            {"start": 1.0, "duration": pytest.approx(2.0), "text": "Hello"}
        ]


# ---------------------------------------------------------------------------
# Upstream errors (4xx/5xx)
# ---------------------------------------------------------------------------


class TestUpstreamHttpErrors:

    @pytest.mark.parametrize("status", [400, 413, 500, 503])
    async def test_http_error_maps_to_asr_failed(
        self, tmp_path: Path, status: int
    ) -> None:
        service = FakeASRService(status_code=status, json_body={"error": "boom"})
        settings = _make_settings(str(tmp_path / "scratch"))
        cache = _make_cache()

        final = await _drive_job(WhisperJobRegistry(), settings, cache, service)

        assert final.status == "error"
        assert final.error_code == errors.ASR_FAILED
        assert str(status) in (final.message or "")
        # The error body snippet survives into the relayable message.
        assert "boom" in (final.message or "")
        # Errors are never cached as transcripts.
        cache.put.assert_not_called()


# ---------------------------------------------------------------------------
# Timeouts
# ---------------------------------------------------------------------------


class TestUpstreamTimeout:

    async def test_read_timeout_maps_to_asr_failed(self, tmp_path: Path) -> None:
        service = FakeASRService(
            raise_exc=httpx.ReadTimeout("The read operation timed out")
        )
        settings = _make_settings(str(tmp_path / "scratch"))
        cache = _make_cache()

        final = await _drive_job(WhisperJobRegistry(), settings, cache, service)

        assert final.status == "error"
        assert final.error_code == errors.ASR_FAILED
        assert "timed out" in (final.message or "")
        cache.put.assert_not_called()


# ---------------------------------------------------------------------------
# Malformed responses
# ---------------------------------------------------------------------------


class TestMalformedResponses:

    async def test_non_json_200_body_is_asr_failed(self, tmp_path: Path) -> None:
        """A 200 with an HTML error page (gateway/proxy rewrite) errors."""
        service = FakeASRService(
            raw_body=b"<html>502 Bad Gateway</html>",
            content_type="text/html",
        )
        settings = _make_settings(str(tmp_path / "scratch"))
        cache = _make_cache()

        final = await _drive_job(WhisperJobRegistry(), settings, cache, service)

        assert final.status == "error"
        assert final.error_code == errors.ASR_FAILED
        cache.put.assert_not_called()

    async def test_empty_200_body_is_asr_failed(self, tmp_path: Path) -> None:
        service = FakeASRService(raw_body=b"", content_type="application/json")
        settings = _make_settings(str(tmp_path / "scratch"))
        cache = _make_cache()

        final = await _drive_job(WhisperJobRegistry(), settings, cache, service)

        assert final.status == "error"
        assert final.error_code == errors.ASR_FAILED
        cache.put.assert_not_called()

    async def test_json_array_200_body_is_asr_failed(self, tmp_path: Path) -> None:
        """A JSON *array* is not an OpenAI transcription object."""
        service = FakeASRService(json_body=["not", "an", "object"])
        settings = _make_settings(str(tmp_path / "scratch"))
        cache = _make_cache()

        final = await _drive_job(WhisperJobRegistry(), settings, cache, service)

        assert final.status == "error"
        assert final.error_code == errors.ASR_FAILED
        cache.put.assert_not_called()

    async def test_json_object_missing_text_completes_empty(
        self, tmp_path: Path
    ) -> None:
        """Tolerated (pinned current behavior): no `text` → empty transcript.

        An OpenAI-compatible service always returns `text`; if one doesn't,
        the job completes with an empty transcript rather than wedging the
        FSM into an error state. If this contract is ever tightened (missing
        text → asr_failed), update this test deliberately.
        """
        service = FakeASRService(json_body={"segments": [], "language": "en"})
        settings = _make_settings(str(tmp_path / "scratch"))
        cache = _make_cache()

        final = await _drive_job(WhisperJobRegistry(), settings, cache, service)

        assert final.status == "done"
        args = cache.put.call_args[0]
        assert args[2] == ""  # empty text
        assert args[3] == []  # empty segments pass through
        assert args[5] == {"detected_language": "en", "duration_sec": 50.0}
