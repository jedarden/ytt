# Deploying ytt on ardenone-cluster (co-hosted with ibkr-mcp)

This document covers the specific deployment of ytt on `ardenone-cluster`
alongside the existing `ibkr-mcp` service on `mcp.ardenone.com`.

For a generic self-hosted deployment (no ardenone specifics), see
[self-hosting.md](self-hosting.md).

## Architecture

```
Cloudflare edge
  └─ Shared Cloudflare Tunnel (062c8e8a-…)
       └─ Traefik (ardenone-cluster)
            ├─ Host(mcp.ardenone.com) + PathPrefix(/ibkr)  → ibkr-mcp (ns ibkr-mcp)
            ├─ Host(mcp.ardenone.com) + PathPrefix(/ytt)   → ytt (ns ytt)
            └─ Host(mcp.ardenone.com)
               + PathPrefix(/.well-known/oauth-protected-resource/ytt)  [priority 1000]
               + PathPrefix(/.well-known/oauth-authorization-server/ytt) [priority 1000]
                   → ytt (ns ytt)  ← additive, ibkr's route unchanged
```

**ytt is purely additive** — it adds new IngressRoute rules and never edits
ibkr's manifests.  ibkr's existing routes continue to function unchanged.

## GitOps path

All changes go through `jedarden/declarative-config` → ArgoCD.  Never
`kubectl apply` directly (ArgoCD `selfHeal` reverts live edits).

```
jedarden/declarative-config
  k8s/ardenone-cluster/ytt/       ← produced by this repo's deploy/ directory
    namespace.yaml
    deployment.yaml
    service.yaml
    pvc.yaml                       (2Gi, longhorn)
    external-secret.yaml           (ESO/OpenBao: ardenone-cluster/ytt/*)
    ingressroute.yaml              (additive; ibkr untouched)
    middlewares.yaml               (ytt-sse, ytt-cors)
    certificate.yaml               (ardenone-ca-issuer → mcp-ardenone-com-tls in ns ytt)
    networkpolicy.yaml             (DNS port-53 always; whisper-openai; broad HTTPS)
    servicemonitor.yaml            (server + canary; Prometheus scrape)
    prometheusrule.yaml            (5 alerts; promtool check rules passes)
    canary-deployment.yaml         (yt-dlp probe; metrics :8081)
    test-deployment.yaml           (sleep infinity; kubectl exec integration tests)
```

## ArgoCD application

The ApplicationSet auto-discovers `k8s/ardenone-cluster/ytt/` and creates
`ytt-ns-ardenone-cluster` (ns `ytt`, `CreateNamespace=true`).

```bash
# Check app status (read-only ArgoCD proxy):
curl -sk https://argocd-ro-ardenone-manager-ts.ardenone.com:8444/api/v1/applications \
  | python3 -m json.tool | grep -A5 '"name":"ytt'
```

## Secrets (OpenBao/ESO)

Paths in OpenBao under `ardenone-cluster/ytt/*`:

| Path | Value |
|------|-------|
| `ardenone-cluster/ytt/oauth-client-secret` | Random 32-byte hex; FastMCP AS token signing key |
| `ardenone-cluster/ytt/oauth-client-id` | `claudeai` (static client name) |
| `ardenone-cluster/ytt/allowed-subjects` | Comma-separated `sub` values (initially empty) |
| `ardenone-cluster/ytt/proxy-url` | Optional Webshare URL (may be empty) |

The `ExternalSecret` creates a K8s Secret named `ytt-secrets` in ns `ytt`.

## .well-known routing (do-no-harm guarantee)

The IngressRoute adds two additive rules with `priority: 1000` for ytt's
OAuth metadata paths.  ibkr's broad `PathPrefix(/.well-known)` rule has no
explicit priority (Traefik auto-computes ~80 from string length).  With priority
1000, ytt's rules always win for `/ytt` suffixes; everything else falls through
to ibkr's rule unchanged.

**Verification after deploy:**

```bash
# ytt metadata — must say /ytt:
curl -s https://mcp.ardenone.com/.well-known/oauth-protected-resource/ytt | python3 -m json.tool
# Expected: "resource": "https://mcp.ardenone.com/ytt"

# ibkr metadata — must be byte-for-byte unchanged from pre-deploy baseline:
curl -s https://mcp.ardenone.com/.well-known/oauth-protected-resource/ibkr | sha256sum
# Compare with the baseline captured before deploy.
```

## Whisper service

ytt connects to `whisper-openai.whisper-stt.svc.cluster.local:8000`.
This is the shared `whisper-openai` Deployment in ns `whisper-stt` on
`ardenone-cluster` (pre-existing, run by whisper-openai for pbx-web).

The NetworkPolicy allows outbound TCP to port 8000 in ns `whisper-stt`.

## Observability

- Metrics: `/ytt/metrics` (ClusterIP, scraped by `ServiceMonitor/ytt`)
- Canary metrics: `:8081` (scraped by `ServiceMonitor/ytt-canary`)
- Alerts: `PrometheusRule/ytt` (5 rules; route via existing Alertmanager receiver)

Check metrics:

```bash
# In-cluster (from any pod):
curl http://ytt.ytt.svc:8080/ytt/metrics | grep ytt_
```

## CI/CD pipeline

1. Push to `jedarden/ytt` on Forgejo.
2. Argo Events `ytt-build` Sensor fires.
3. `ytt-build` WorkflowTemplate runs: `pytest -m "not integration"` → kaniko build
   → push to `ghcr.io/jedarden/ytt:<tag>` → bump tag in declarative-config.
4. ArgoCD syncs the updated Deployment; pod restarts with the new image.

Watch builds: https://argo-ci.ardenone.com

## Rollback

```bash
cd ~/jedarden-declarative-config
git revert HEAD    # reverts the tag-bump commit
git push
# ArgoCD syncs; Deployment rolls back to previous image.
```

Never `kubectl rollout undo` (ArgoCD selfHeal reverts it immediately).

## Running integration tests in-cluster

```bash
# Via the ytt-test Deployment:
kubectl --server=http://traefik-ardenone-cluster:8001 \
  exec -n ytt deploy/ytt-test -- \
  env YTT_TEST_TOKEN=<bearer-token> ytt test --integration
```

Or interactively:
```bash
kubectl --server=http://traefik-ardenone-cluster:8001 \
  exec -it -n ytt deploy/ytt-test -- /bin/bash
# Inside the pod:
export YTT_TEST_TOKEN=<token>
ytt test --integration
```
