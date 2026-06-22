# ytt Deploy Artifacts

This directory contains the Kubernetes manifests and Argo Workflows CI templates
for `ytt` on `ardenone-cluster`.

## Where these files go

**These files must be copied into `jedarden/declarative-config`**, not applied
directly.  ArgoCD's `selfHeal` reverts any live `kubectl apply`, so the GitOps
path is the only way to make changes stick.

| File(s) | Destination in `jedarden/declarative-config` |
|---------|----------------------------------------------|
| `k8s/ardenone-cluster/ytt/*.yaml` | `k8s/ardenone-cluster/ytt/` (new directory) |
| `argo-workflows/iad-ci/ytt-build-workflowtemplate.yaml` | `k8s/iad-ci/argo-workflows/ytt-build-workflowtemplate.yaml` |
| `argo-workflows/iad-ci/ytt-build-sensor.yaml` | `k8s/iad-ci/argo-events/ytt-build-sensor.yaml` |

After copying, commit + push to Forgejo.  ArgoCD auto-discovers
`k8s/ardenone-cluster/ytt/` via the ApplicationSet and creates the
`ytt-ns-ardenone-cluster` Application (`CreateNamespace=true`).

## The additive `.well-known` routing (do NOT touch ibkr manifests)

`ytt`'s `IngressRoute` adds two higher-`priority` root rules that intercept
only ytt's OAuth metadata suffixes:

```
PathPrefix(`/.well-known/oauth-protected-resource/ytt`)
PathPrefix(`/.well-known/oauth-authorization-server/ytt`)
```

These are **more specific + explicitly higher priority** than ibkr's broad
`PathPrefix(/.well-known)` rule, so Traefik routes only ytt's paths here
while everything else (ibkr's metadata, bare-root probes) continues to hit
ibkr's existing rule **unchanged**.  ibkr's manifests are **never edited**.

## Human-gated steps

See `DEPLOY-CHECKLIST.md` for the complete ordered list of human-gated
operations (OpenBao secret writes, connector add, WAF rule, etc.).
