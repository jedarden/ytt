#!/usr/bin/env bash
# Definition of done for ytt: the unit test suite, exactly as documented in
# CONTRIBUTING.md. Bare `pytest` is not reliably runnable on every host (the
# ~/.local/bin shim assumes /usr/bin/python3), so this drives it through uv.
set -euo pipefail
cd "$(dirname "$0")/.."
exec uv run pytest -m "not integration" -q
