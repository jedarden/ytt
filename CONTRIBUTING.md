# Contributing to ytt

Thank you for your interest in contributing!

## Development setup

```bash
git clone https://git.ardenone.com/jedarden/ytt.git
cd ytt
uv sync           # install exact locked deps (Python 3.12 pinned)
uv run pytest -m "not integration" -q    # run the unit suite
```

Requirements: Python 3.12, `uv`, `ffmpeg` (for the audio path in Whisper tests).

## Running tests

```bash
# Unit tests (runs anywhere, no network):
uv run pytest -m "not integration" -q

# Integration tests (only pass in-cluster — ardenone-cluster residential egress):
# kubectl exec -n ytt deploy/ytt-test -- ytt test --integration
```

Integration tests hit real YouTube URLs and the in-cluster Whisper service.
They will NOT pass from a datacenter IP (the server machine included).
Do not chase integration test failures locally — verify in-cluster.

## Submitting changes

1. Fork the repo on Forgejo (`https://git.ardenone.com/jedarden/ytt`).
2. Create a feature branch.
3. Run the unit suite (`pytest -m "not integration" -q`) — it must pass.
4. Submit a pull request.

The CI pipeline (Argo Workflows on `iad-ci`) runs the unit suite + builds the
image on every push.  GitHub Actions are disabled fleet-wide.

## Code style

- Python 3.12; typed (type hints required on all public functions).
- Format with `ruff format` (via `uv run ruff format .`).
- Lint with `ruff check` (via `uv run ruff check .`).
- All modules have docstrings referencing the relevant plan section.

## Reporting bugs and security issues

Security issues: see [SECURITY.md](SECURITY.md).
Other bugs: open an issue on Forgejo or GitHub.

## Dependency pinning

All deps are pinned exactly in `uv.lock`.  To update a dep:
```bash
uv add <package>==<version>   # updates pyproject.toml + uv.lock
```

Commit both `pyproject.toml` and `uv.lock`.

## Architecture reference

See `docs/plan/plan.md` for the complete specification.  Do not modify `plan.md`
(it is the authoritative design doc; deviations should be noted in PR description).
