#!/usr/bin/env bash
# Definition of done for ytt: the unit test suite, exactly as documented in
# CONTRIBUTING.md. Bare `pytest` is not reliably runnable on every host (the
# ~/.local/bin shim assumes /usr/bin/python3), so this drives it through uv.
#
# The deploy/ <-> declarative-config mirror parity guard
# (tests/unit/test_deploy_parity.py) runs first, separately, so divergence
# from the applied manifests fails with an unmistakable message instead of
# one line mid-suite. It skips (exit 0) where no declarative-config checkout
# exists — CI image builds and clean extractions; on hosts that have the
# sibling ../declarative-config checkout (or $YTT_DECLARATIVE_CONFIG_DIR) it
# is the enforcement point for the "keep the two copies identical" rule in
# docs/notes/single-replica.md.
set -euo pipefail
cd "$(dirname "$0")/.."
# pytest >= 9 exits 5 (NO_TESTS_COLLECTED) for the module-level skip in a
# clean extraction, which `set -e` would turn into a bogus gate failure —
# the skip is the guard saying "nothing to enforce here", not a red suite.
# Tolerate exactly 5; any other non-zero exit (real divergence = 1,
# collection error = 2) still fails the gate.
set +e
uv run pytest tests/unit/test_deploy_parity.py -q
parity_status=$?
set -e
if [ "$parity_status" -ne 0 ] && [ "$parity_status" -ne 5 ]; then
  exit "$parity_status"
fi
exec uv run pytest -m "not integration" -q
