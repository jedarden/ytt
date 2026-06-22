# ytt Deploy Checklist (human-gated steps)

All steps that require credentials, cluster access, UI actions, or
irreversible host-wide changes.  Complete them in order.

## Pre-flight: ibkr baseline capture

Before changing anything, record the current state of ibkr:

```bash
# From the ardenone-cluster network (kubectl exec or in-cluster)
curl -s https://mcp.ardenone.com/.well-known/oauth-protected-resource/ibkr | sha256sum
curl -s https://mcp.ardenone.com/.well-known/oauth-authorization-server/ibkr | sha256sum
# Perform a basic ibkr tool call and record the response hash.
# Save these hashes; compare again at step 9 (ibkr regression gate).
```

## 1. Write secrets to OpenBao (ardenone-cluster)

```bash
# From ardenone-cluster (needs OpenBao write access):

# OAuth client secret (the FastMCP AS uses this to sign tokens)
vault kv put ardenone-cluster/ytt/oauth-client-secret \
  value="<generate a random 32-byte hex secret>"

# OAuth client id (must match the registered client in auth.py → static client "claudeai")
vault kv put ardenone-cluster/ytt/oauth-client-id \
  value="claudeai"

# Subject allowlist (comma-separated; discovered via `ytt selftest --show-sub` after first OAuth flow)
vault kv put ardenone-cluster/ytt/allowed-subjects \
  value=""

# Optional: Webshare proxy URL (only if residential egress ever burns)
vault kv put ardenone-cluster/ytt/proxy-url \
  value=""
```

Note: The `ExternalSecret` references these paths under `ardenone-cluster/ytt/*`.

## 2. GHCR push credential for iad-ci

The `ytt-build` WorkflowTemplate pushes to `ghcr.io/jedarden/ytt`.
iad-ci needs a GitHub token with `write:packages` scope.

```bash
# If iad-ci has ESO: create an ExternalSecret + OpenBao entry.
# If iad-ci does NOT have ESO (likely — it is the CI cluster):
kubectl --kubeconfig=/home/coding/.kube/iad-ci.kubeconfig \
  create secret generic ghcr-push-token \
  --from-literal=token="<github-token-with-write:packages>" \
  -n argo-workflows
```

Verify: `kubectl ... get secret ghcr-push-token -n argo-workflows`

## 3. Copy manifests into declarative-config

```bash
cd ~/jedarden-declarative-config  # your local checkout
cp -r ~/ytt/deploy/k8s/ardenone-cluster/ytt k8s/ardenone-cluster/ytt
cp ~/ytt/deploy/argo-workflows/iad-ci/ytt-build-workflowtemplate.yaml \
   k8s/iad-ci/argo-workflows/ytt-build-workflowtemplate.yaml
cp ~/ytt/deploy/argo-workflows/iad-ci/ytt-build-sensor.yaml \
   k8s/iad-ci/argo-events/ytt-build-sensor.yaml
git add -A && git commit -m "add ytt manifests and CI templates"
git push
```

## 4. First CI run (build the image)

Trigger the `ytt-build` WorkflowTemplate manually to build and push the first
image before the Deployment can start:

```bash
kubectl --kubeconfig=/home/coding/.kube/iad-ci.kubeconfig create -f - <<EOF
apiVersion: argoproj.io/v1alpha1
kind: Workflow
metadata:
  generateName: ytt-build-manual-
  namespace: argo-workflows
spec:
  arguments:
    parameters:
    - name: tag
      value: "0.1.0"
    - name: ref
      value: "refs/heads/main"
  workflowTemplateRef:
    name: ytt-build
EOF
```

Watch: Argo UI at https://argo-ci.ardenone.com

## 5. ArgoCD: force sync

After the ApplicationSet discovers `k8s/ardenone-cluster/ytt/`:

```bash
# Via read-only ArgoCD API (just to check status):
curl -sk https://argocd-ro-ardenone-manager-ts.ardenone.com:8444/api/v1/applications \
  | python3 -m json.tool | grep -A3 '"name": "ytt'
```

If the app isn't discovered yet, force a refresh via the ArgoCD UI
(argocd-manager direct kubeconfig):

```bash
kubectl --kubeconfig=/home/coding/.kube/ardenone-manager.kubeconfig \
  -n argocd patch application ytt-ns-ardenone-cluster \
  --type merge -p '{"operation": {"sync": {}}}'
```

## 6. Wait for ExternalSecret to materialize

```bash
kubectl --server=http://traefik-ardenone-cluster:8001 \
  get externalsecret -n ytt
# STATUS must be "SecretSynced"
# The Deployment will CrashLoop until the Secret is present.
```

## 7. Verify ytt health

```bash
curl -s https://mcp.ardenone.com/ytt/health
# Expect: {"status": "ok"}
```

If `health` returns 404, check Traefik IngressRoute and cert-manager Certificate:

```bash
kubectl --server=http://traefik-ardenone-cluster:8001 \
  get ingressroute,certificate -n ytt
```

## 8. Verify OAuth metadata

```bash
curl -s https://mcp.ardenone.com/.well-known/oauth-protected-resource/ytt | python3 -m json.tool
# Expect "resource": "https://mcp.ardenone.com/ytt"

curl -s https://mcp.ardenone.com/.well-known/oauth-authorization-server/ytt | python3 -m json.tool
# Expect "issuer": "https://mcp.ardenone.com/ytt"
```

## 9. ibkr regression gate (DO NOT SKIP)

Re-run the ibkr checks from step 0 and verify byte-for-byte unchanged:

```bash
curl -s https://mcp.ardenone.com/.well-known/oauth-protected-resource/ibkr | sha256sum
curl -s https://mcp.ardenone.com/.well-known/oauth-authorization-server/ibkr | sha256sum
```

If ANY hash differs → immediately roll back ytt:

```bash
cd ~/jedarden-declarative-config
git revert HEAD  # reverts the ytt manifest commit
git push
# ArgoCD syncs; ytt pod terminates
```

**Do not proceed to step 10 if ibkr is affected.**

## 10. Discover the OAuth subject (`sub`)

Add the connector in Claude Desktop.  After the OAuth flow completes, in the
ytt pod:

```bash
kubectl --server=http://traefik-ardenone-cluster:8001 \
  exec -n ytt deploy/ytt -- ytt selftest --show-sub
```

Copy the printed `sub` value.  Update the OpenBao secret:

```bash
vault kv put ardenone-cluster/ytt/allowed-subjects \
  value="<the-sub-value>"
```

The ExternalSecret will resync and the Deployment will reload (the settings
are env-based; a rolling restart triggers automatically via a checksum
annotation on the Secret if configured, else restart manually):

```bash
# Force pod restart to pick up the new env
kubectl --server=http://traefik-ardenone-cluster:8001 \
  rollout restart deploy/ytt -n ytt
```

Wait for selfHeal to notice the rollout and let it settle.  *(ArgoCD selfHeal
normally reverts live mutations; the restart is to pick up the new Secret value,
which ArgoCD-managed Deployments do handle via Secret updates triggering pod
recreation. If selfHeal reverts the rollout, the pod will restart naturally when
the Secret is re-applied.)*

## 11. Verify tool call works end-to-end

In Claude Desktop (after adding the connector), ask:
> "Get the transcript of https://www.youtube.com/watch?v=jNQXAC9IVRw"

Expected: the transcript of "Me at the zoo" (first YouTube video, 18s).

## 12. Verify on mobile

Open Claude iOS/Android.  The connector added on Desktop should already be
available (shared token).  Send the same request and verify the response.

## 13. (Optional) Cloudflare WAF allowlist

If you want host-level IP restriction for `mcp.ardenone.com`:
- Cloudflare Dashboard → `mcp.ardenone.com` → Security → WAF → Custom Rules
- Allow: `ip.src in {160.79.104.0/21}` (Anthropic IPv4) +
         `ip6.src in {2607:6bc0::/48}` (Anthropic IPv6)
- Block all other traffic to `mcp.ardenone.com`
- **Verify ibkr still works from Claude after the rule is active** (both
  are Claude connectors on the same host → same Anthropic IP range → rule is
  shared-host compatible). If ibkr breaks, disable the rule.

## 14. Set GHCR package public

1. GitHub → https://github.com/jedarden/ytt/packages
2. Find `ghcr.io/jedarden/ytt`
3. Package Settings → Visibility → Public
4. Link to repository `jedarden/ytt`

Verify: `docker pull ghcr.io/jedarden/ytt:0.1.0` from a clean machine without
GitHub auth.

## 15. Tag the first release

```bash
cd ~/ytt
git tag -a v0.1.0 -m "ytt v0.1.0 — initial release"
git push  # pushes to Forgejo; mirror sync sends to GitHub
```

Verify the tag appears on GitHub (mirror lag: up to 8 h, usually <1 min on
`sync_on_commit`).  Create a GitHub Release from the tag UI if desired.
