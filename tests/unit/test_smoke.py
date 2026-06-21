"""Phase 0 scaffold smoke tests: the package imports and the CLI is wired."""

from __future__ import annotations

import importlib

import pytest

import ytt
from ytt.cli import main

# Every module in the Deliverables tree must at least import cleanly.
_MODULES = [
    "ytt.cli",
    "ytt.config",
    "ytt.server",
    "ytt.canonicalize",
    "ytt.fetch",
    "ytt.parse_json3",
    "ytt.whisper",
    "ytt.cache",
    "ytt.auth",
    "ytt.authz",
    "ytt.ratelimit",
    "ytt.errors",
    "ytt.models",
    "ytt.observability",
    "ytt.selftest",
    "ytt.canary",
]


def test_version_is_set():
    assert isinstance(ytt.__version__, str)
    assert ytt.__version__


@pytest.mark.parametrize("module", _MODULES)
def test_module_imports(module):
    assert importlib.import_module(module) is not None


def test_cli_help_exits_zero(capsys):
    rc = main([])
    captured = capsys.readouterr()
    assert rc == 0
    assert "usage: ytt" in captured.out


def test_cli_version_flag(capsys):
    rc = main(["--version"])
    captured = capsys.readouterr()
    assert rc == 0
    assert ytt.__version__ in captured.out


def test_error_codes_present():
    from ytt import errors

    # A sampling of the stable taxonomy must exist as constants.
    for code in ("BAD_URL", "PRIVATE", "IP_BLOCKED", "EMPTY_BODY", "ASR_FAILED", "NOT_FOUND"):
        assert hasattr(errors, code)
