# ytt CI/CD Infrastructure (Phase 9)

## Overview

Phase 9 implements the in-cluster integration test harness for ytt, enabling acceptance scenario validation against real YouTube and Whisper services in `ardenone-cluster` (residential egress required; datacenter IPs are blocked by YouTube).

## Components

### 1. CLI Test Commands

```bash
# Run unit tests (anywhere - no external dependencies)
ytt test --unit
# or simply: ytt test (default)

# Run integration tests (in-cluster only - requires residential egress)
ytt test --integration
```

Both commands emit a one-line JSON summary to stdout:
```json
{"suite": "unit", "exit_code": 0}
```

### 2. Argo Workflow Template

**Location:** `deploy/argo-workflows/iad-ci/ytt-build-workflowtemplate.yaml`

**Workflow Steps:**
1. **run-unit-tests** - Fast-fail unit test suite (`pytest -m "not integration"`)
2. **docker-build-push** - Multi-stage Docker build + push to GHCR `ghcr.io/jedarden/ytt:<tag>`
3. **bump-declarative-config** - Auto-bump image tag in `jedarden/declarative-config`
4. **run-integration-tests** - In-cluster acceptance tests via `kubectl exec`

**Integration Test Step:**
- Waits for `ytt-test` deployment rollout
- Executes `ytt test --integration` in a `ytt-test` pod
- Uses ClusterIP-scoped targeting (no Traefik/Cloudflare load)
- Requires `YTT_TEST_TOKEN` secret (OAuth bearer token)

### 3. Test Deployment

**Location:** `deploy/k8s/ardenone-cluster/ytt/test-deployment.yaml`

The `ytt-test` deployment provides a long-lived pod for integration testing:
- Runs `sleep infinity` (waits for `kubectl exec`)
- Has in-cluster connectivity to `ytt` ClusterIP Service
- Has in-cluster connectivity to `whisper-openai.whisper-stt.svc`
- Configured with test environment variables

**Usage:**
```bash
# Manual execution in a test pod
kubectl exec -n ytt deploy/ytt-test -c ytt-test -- \
  env YTT_TEST_TOKEN="<token>" ytt test --integration
```

### 4. Integration Test Suite

**Location:** `tests/integration/`

**Coverage:**
- ✅ Scenario 1: Captioned, short (inline + cache hit)
- ✅ Scenario 2: Captioned, long (chunked pagination)
- ✅ Scenario 3: Auto-captioned rolling (dedup)
- ✅ Scenario 4: No captions (Whisper ASR fallback)
- ✅ Scenario 5: Concurrent same-video (single-flight)
- ✅ Scenario 6: Cache pressure (byte cap + LRU)
- ✅ Scenario 8: Error taxonomy (private/age/livestream/region/too_long)
- ✅ Scenario 11: URL forms (canonicalization)
- ✅ Scenario 12: Dependency-down (Whisper 5xx)
- ✅ Scenario 14: Co-hosting isolation + ibkr unchanged
- ✅ Load test: Queue saturation (429 + Retry-After)

**Test Fixtures:**
- `VIDEO_SHORT_CAPTIONED` = "jNQXAC9IVRw" (Me at the zoo, 18s)
- `VIDEO_LONG_CAPTIONED` = "dQw4w9WgXcQ" (Never Gonna Give You Up, 3m32s)
- `VIDEO_AUTO_CAPTIONED` = "BaW_jenozKc" (Numberphile)
- `VIDEO_NO_CAPTIONS` = "0EqSXDwTq6s" (Charlie bit my finger)
- `VIDEO_PRIVATE` = "zTD2RZz6mlo"
- `VIDEO_LIVESTREAM` = "21X5lGlDOfg"
- `VIDEO_TOO_LONG_FOR_ASR` = "tSugne__doU" (4h lecture)

## Prerequisites

### In iad-ci (CI cluster)

**Secrets Required:**
```bash
# GHCR push credential (GitHub token with write:packages)
kubectl -n argo-workflows create secret generic ghcr-push-token \
  --from-file=config.json=/path/to/config.json

# Forgejo push credential (for declarative-config commits)
kubectl -n argo-workflows create secret generic forgejo-push-token \
  --from-file=.git-credentials=/path/to/git-credentials

# Ardenone-manager kubeconfig (read-only for integration test execution)
kubectl -n argo-workflows create configmap ardenone-manager-kubeconfig \
  --from-file=ardenone-manager.kubeconfig=/path/to/kubeconfig

# OAuth test token (bearer token with subject in YTT_ALLOWED_SUBJECTS)
kubectl -n argo-workflows create secret generic ytt-test-token \
  --literal=token="<bearer-token>"
```

### In ardenone-cluster (target cluster)

**Deployment Required:**
- Apply manifests from `deploy/k8s/ardenone-cluster/ytt/` via ArgoCD
- Ensure `ytt` namespace exists
- Ensure `ytt-test` deployment is running
- Ensure `ytt` ClusterIP Service is accessible

**Secrets Required (via ExternalSecret):**
- `YTT_OAUTH_CLIENT_ID` / `YTT_OAUTH_CLIENT_SECRET` (Authentik OAuth)
- `YTT_ALLOWED_SUBJECTS` (comma-separated subject list)
- `YTT_JWT_SIGNING_SECRET` (FastMCP token signing)

## Execution

### Manual Integration Test (In-Cluster)

```bash
# From within ardenone-cluster network context
kubectl exec -n ytt deploy/ytt-test -c ytt-test -- \
  env YTT_TEST_TOKEN="<token>" ytt test --integration
```

### Automated (via Argo Workflow)

The workflow automatically:
1. Builds and pushes the image
2. Updates declarative-config
3. Waits for ytt-test rollout
4. Runs integration tests in-cluster

**Trigger:**
```bash
kubectl -n argo-workflows create -f - <<EOF
apiVersion: argoproj.io/v1alpha1
kind: Workflow
metadata:
  generateName: ytt-build-
spec:
  workflowTemplateRef:
    name: ytt-build
  arguments:
    parameters:
      - name: tag
        value: "0.1.0"
      - name: ref
        value: "refs/heads/main"
EOF
```

## Architecture

```
┌─────────────────┐
│  iad-ci (CI)    │
└────────┬────────┘
         │ 1. build+push image
         │ 2. bump declarative-config
         │ 3. kubectl exec (ardenone-manager)
         ▼
┌─────────────────────────┐
│  ardenone-cluster       │
│  ┌───────────────────┐  │
│  │ ytt-test pod      │  │
│  │ (ytt test --int)  │  │
│  └───────┬───────────┘  │
│          │ ClusterIP    │
│          ▼              │
│  ┌───────────────────┐  │
│  │ ytt Service       │  │
│  │ (ClusterIP)       │  │
│  └───────┬───────────┘  │
│          │               │
│  ┌───────▼───────────┐  │
│  │ ytt Deployment    │  │
│  └───────────────────┘  │
└─────────────────────────┘
```

**Key Design Points:**
- Integration tests target ClusterIP (not Traefik/Cloudflare)
- No load on shared ingress (do-no-harm to ibkr)
- Residential egress via ardenone-cluster
- Tests require valid OAuth token (subject allowlist)

## Troubleshooting

**Integration tests skip with "YTT_TEST_TOKEN not set":**
- Create `ytt-test-token` secret in argo-workflows namespace
- Token must have subject in `YTT_ALLOWED_SUBJECTS`

**Integration tests fail with "ytt server unreachable":**
- Verify ytt deployment is running in ardenone-cluster
- Check ytt-test pod can resolve `ytt.ytt.svc.cluster.local`
- Verify ClusterIP Service exists

**YouTube blocked (datacenter IP):**
- Integration tests MUST run in ardenone-cluster
- Cannot run from EX44 or iad-ci (datacenter IPs blocked)

**Whisper tests fail:**
- Verify `whisper-openai.whisper-stt.svc` is reachable
- Check model `Systran/faster-whisper-small` is pulled

**Workflow fails at integration step:**
- Check `ardenone-manager-kubeconfig` configmap exists
- Verify kubeconfig has read access to ytt namespace
- Check ytt-test deployment rollout status

## Status

✅ Phase 9 Implementation Complete:
- ✅ CLI test commands (`ytt test --unit|--integration`)
- ✅ Unit suite wired into Argo CI
- ✅ Integration tests covering scenarios 1-6, 8, 11, 12, 14
- ✅ Load test (ClusterIP-scoped queue saturation)
- ✅ ibkr smoke test (do-no-harm verification)
- ✅ Test deployment (ytt-test)
- ✅ Workflow template with all 4 steps
- ✅ ClusterIP-scoped targeting (no shared ingress load)
