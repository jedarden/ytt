#!/usr/bin/env bash
# Definition of done for ytt: the unit test suite, exactly as documented in
# CONTRIBUTING.md. Bare `pytest` is not reliably runnable on every host (the
# ~/.local/bin shim assumes /usr/bin/python3), so this drives it through uv.
#
# The release-metadata drift guard runs before anything else: every file that
# advertises the release version must agree with VERSION. The 0.2.15–0.2.20
# window shipped half-bumped twice over (bead ytt-d18f0ab1) — 0.2.20 bumped
# only VERSION while the README quick start, the self-hosting compose image,
# pyproject.toml, uv.lock and ytt.__version__ stayed at 0.2.19, and nothing
# after 0.2.16 got a git tag — the same drift class that produced the
# 0.2.13/0.2.14 backfill bead (ytt-8efb9b9d). Its failures are one-liners
# naming the file to fix, so they must not drown in suite output.
#
# The deploy/ <-> declarative-config mirror parity guard
# (tests/unit/test_deploy_parity.py) runs first, separately, so divergence
# from the applied manifests fails with an unmistakable message instead of
# one line mid-suite. It skips (exit 0) where no declarative-config checkout
# exists — CI image builds and clean extractions; on hosts that have the
# sibling ../declarative-config checkout (or $YTT_DECLARATIVE_CONFIG_DIR) it
# is the enforcement point for the "keep the two copies identical" rule in
# docs/notes/single-replica.md.
#
# The bead-status documentation guard (tests/unit/test_bead_inventory_docs.py)
# runs inside the suite below: it cross-checks the generated
# docs/bead-inventory.{md,json} pair and lints curated docs for hand-written
# bead-status claims; its freshness leg skips where no live bead store exists
# (same skip shape as the parity guard above).
#
# The egress-boundary guard (tests/unit/test_egress_boundary.py) also runs
# inside the suite: static legs close the dependency / installed-plugin /
# package-import / Settings-URL surfaces against third-party transcript APIs
# and PoToken providers, and mocked-network drives of the caption and ASR
# paths record every egress under a socket-level tripwire — enforcing the
# documented no-third-party, cookie-free promise (README intro,
# docs/notes/proxy-egress.md).
#
# The configuration-documentation drift guard
# (tests/unit/test_config_docs_drift.py) runs inside the suite too: it holds
# the README/configuration-guide tables, the self-hosting quick-start and
# compose examples, and the deploy/ manifests to the actual Settings schema —
# documented defaults and required-variable markers, the documented
# fail-closed validator claims, and the manifest image pins (the manifest
# legs of the release-pin check above, which the Docker build gate's plain
# pytest run would otherwise never see — the two markdown pins are enforced
# inside the suite by the same module, for the same reason).
#
# The documentation-reference drift guard
# (tests/unit/test_docs_reference_drift.py) also runs inside the suite:
# relative links and GitHub heading anchors across README, CONTRIBUTING,
# SECURITY, CHANGELOG and docs/ must resolve; backticked tests/ scripts/
# deploy/ ytt/ docs/ citations must exist as paths (the wrong-extension and
# renamed-test class); and — on hosts with a live bead store, same skip
# shape as the parity guard — every ytt-XXXXXXXX bead ID cited in curated
# docs must exist in the store.
set -euo pipefail
cd "$(dirname "$0")/.."
# --- release-metadata drift guard ------------------------------------------
release_version="$(cat VERSION 2>/dev/null || true)"
drift=""
note() { drift+="  - $1"$'\n'; }

if ! printf '%s\n' "$release_version" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$'; then
  echo "release-metadata guard: VERSION is missing or not X.Y.Z: '${release_version}'" >&2
  exit 1
fi

changelog_top="$(grep -m1 -E '^## \[[0-9]+\.[0-9]+\.[0-9]+\]' CHANGELOG.md | sed -E 's/^## \[([0-9]+\.[0-9]+\.[0-9]+)\].*/\1/' || true)"
[ "$changelog_top" = "$release_version" ] ||
  note "CHANGELOG newest section is '${changelog_top:-none}', VERSION is ${release_version} (CHANGELOG.md)"

for doc in README.md docs/usage/self-hosting.md; do
  pins="$(grep -oE 'ronaldraygun/ytt:[0-9]+\.[0-9]+\.[0-9]+' "$doc" | sort -u || true)"
  pin_count="$(printf '%s' "$pins" | grep -c . || true)"
  if [ "$pin_count" -ne 1 ]; then
    note "${doc} should pin exactly one ronaldraygun/ytt:X.Y.Z image, found ${pin_count}"
  elif [ "$pins" != "ronaldraygun/ytt:${release_version}" ]; then
    note "${doc} pins '${pins}', VERSION is ${release_version}"
  fi
done

pyproject_version="$(sed -nE 's/^version = "([0-9]+\.[0-9]+\.[0-9]+)".*/\1/p' pyproject.toml | head -1)"
[ "$pyproject_version" = "$release_version" ] ||
  note "pyproject.toml version is '${pyproject_version:-none}', VERSION is ${release_version}"

init_version="$(sed -nE 's/^__version__ = "([^"]+)".*/\1/p' ytt/__init__.py | head -1)"
[ "$init_version" = "$release_version" ] ||
  note "ytt/__init__.py __version__ is '${init_version:-none}', VERSION is ${release_version}"

lock_version="$(grep -A1 '^name = "ytt"$' uv.lock | sed -nE 's/^version = "([^"]+)".*/\1/p' | head -1)"
[ "$lock_version" = "$release_version" ] ||
  note "uv.lock ytt entry is '${lock_version:-none}', VERSION is ${release_version} (regen with: uv lock)"

# Newest v* tag must be the release. Both directions have bitten us: nothing
# after 0.2.16 was tagged at all (the drift this guard exists for), and a tag
# ahead of VERSION would mean a release was tagged without its bump commit.
# The leg skips in clean extractions — `git archive` carries no .git, so the
# guard would fail there for the wrong reason.
if [ -d .git ] && git rev-parse --git-dir >/dev/null 2>&1; then
  newest_tag="$(git tag -l 'v[0-9]*' --sort=-v:refname | head -1)"
  if [ -n "$newest_tag" ] && [ "$newest_tag" != "v${release_version}" ]; then
    note "newest git tag is ${newest_tag}, VERSION is ${release_version} — if tags are merely stale locally, git fetch --tags; if the release genuinely shipped untagged, backfill the annotated tag at its VERSION-bump commit (see the 0.2.20 CHANGELOG entry)"
  fi
fi

if [ -n "$drift" ]; then
  echo "release-metadata drift — these disagree with VERSION ${release_version}:" >&2
  printf '%s' "$drift" >&2
  echo "Bump every copy in the same release commit — the six version-bearing files (VERSION, pyproject.toml, uv.lock, ytt/__init__.py, README.md, docs/usage/self-hosting.md) plus the CHANGELOG section and the v<version> tag." >&2
  exit 1
fi
# --- end release-metadata drift guard --------------------------------------
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
