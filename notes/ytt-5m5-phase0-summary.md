# Phase 0 Completion Summary: Project Scaffold + CI Build Pipeline

## Completed Tasks

### 1. Project Structure ✓
- **pyproject.toml**: Configured with Python 3.12, FastMCP ≥2.11.1, yt-dlp==2025.5.22, uvicorn, httpx, pydantic-settings, prometheus-client, structlog
- **uv.lock**: Committed with all dependencies pinned
- **ytt package skeleton**: Complete with 20+ modules including auth, cache, fetch, server, etc.

### 2. Dockerfile ✓
Multi-stage build with:
- **base stage**: python:3.12-slim + ffmpeg (required for yt-dlp bestaudio) + uv
- **builder stage**: Full dependency resolution (incl. dev) for testing
- **test stage**: Runs `pytest -m "not integration"` as build gate
- **runtime stage**: Runtime deps only, non-root user, CMD `["ytt","serve"]`
- OCI labels linking back to GitHub repo

### 3. Cross-Repo CI Pipeline ✓

#### ytt-build WorkflowTemplate
Location: `declarative-config/k8s/iad-ci/argo-workflows/ytt-build.yaml`
- Clones jedarden/ytt repo
- Builds with Kaniko executor
- Pushes to GHCR: `ghcr.io/jedarden/ytt:<sha>` and `:latest`
- Runs pytest as part of Docker build (test gate)
- Uses ghcr-registry secret for push auth

#### ytt-sensor
Location: `declarative-config/k8s/iad-ci/argo-events/ytt-sensor.yml` (NEW)
- Listens for push events to jedarden/ytt master branch
- Triggers ytt-build WorkflowTemplate
- Ignores CI auto-bump commits to prevent cascade loops
- Passes commit SHA as parameter

#### GitHub Webhook EventSource
Updated: `declarative-config/k8s/iad-ci/argo-events/github-eventsource.yml`
- Added ytt entry with endpoint `/ytt`
- Configured for push events
- Uses shared github-webhook-secret

## Phase 0 Exit Criteria Met

✅ Dockerfile syntax valid (multi-stage, ffmpeg, test gate, CMD ytt serve)
✅ pyproject.toml + uv.lock present and committed
✅ ytt package skeleton exists with comprehensive modules
✅ ytt-build WorkflowTemplate passes structural validation
✅ ytt-sensor created and configured
✅ GitHub eventsource updated with ytt webhook

## Next Steps

Phase 0 deliverables are complete. The CI pipeline is ready to:
1. Build on push to master
2. Run unit tests via pytest
3. Push image to GHCR with commit SHA tag

The first image build will occur on the next push to master, or can be triggered manually via Argo Workflows UI.

## Files Modified

### ytt repo
- No changes (project already scaffolded)

### declarative-config repo
- `k8s/iad-ci/argo-events/ytt-sensor.yml` (NEW)
- `k8s/iad-ci/argo-events/github-eventsource.yml` (MODIFIED - added ytt)
- `k8s/iad-ci/argo-workflows/ytt-build.yaml` (already existed)

---
*Phase 0 completed 2025-06-25*
