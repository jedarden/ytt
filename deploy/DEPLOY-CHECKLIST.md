# ytt Deploy Checklist (human-gated steps)

All steps that require credentials, cluster access, UI actions, or otherwise
cannot be done from inside a manifest.  The release path is: **bump `VERSION`
→ CI builds + pushes the image → bump the pinned tag in `declarative-config`
→ ArgoCD syncs.**

> This checklist covers *building and pinning* a release.  What happens in the
> cluster during the swap, post-deploy validation, and rollback is the
> operator runbook: [RUNBOOK.md](RUNBOOK.md).

> This checklist was rewritten 2026-09-16 to match the applied state.  The
> pre-0.1.0 first-deploy procedure (GHCR publishing, Google-federated OAuth,
> `ytt-test` canary harness) is history — see git log if you need it.

## Release SOP (each release)

### 1. Bump the version in the release commit

CI (`ytt-build` in iad-ci) fires on **every push to `master`** and fails the
run unless the pushed commit changes `VERSION`.  Bump all four in the same
commit:

- `VERSION` (this is the one CI validates; the image is tagged with it)
- `pyproject.toml` `version`
- `ytt/__init__.py` `__version__`
- `CHANGELOG.md` (new `## [x.y.z]` section)

### 2. Push — CI builds and publishes the image

```bash
git push   # Forgejo is origin; GitHub receives the read-only mirror
```

The Forgejo webhook fires the `ytt-sensor` (argo-events) → `ytt-build`
WorkflowTemplate in iad-ci. The sensor passes the pushed commit's SHA
(`body.after`) as the workflow's `revision` parameter, and every step works
on exactly that commit — not on whatever `master` has become by the time a
pod starts:

1. `resolve-version` rejects anything that is not a full SHA, checks out that
   commit, and validates that *it* bumps `VERSION` (semver) — no bump, no
   build.
2. `docker-build` builds that commit with kaniko (an initContainer checks out
   the exact SHA); the Dockerfile's test stage runs
   `pytest -m "not integration"` against the whole build context and a red
   suite aborts the build; then it pushes **`ronaldraygun/ytt:<version>`** to
   Docker Hub — the image the cluster deploys.
3. `publish-ghcr` copies that exact image to
   **`ghcr.io/jedarden/ytt:<version>`** (`skopeo copy --preserve-digests`; no
   second build) — the tag the README quick start pins, and the one a
   self-hoster can pull with no login. It refuses to succeed unless both
   registries report the same manifest digest.
4. `quick-start-smoke` boots the GHCR tag as a pod with the README quick-start
   environment, and requires `/ytt/health` to answer `{"status":"ok"}` and an
   anonymous GET on the MCP mount to get the documented `401` + Bearer
   `WWW-Authenticate` challenge.
5. `verify-ghcr-public` — last on purpose — asks the registry for the tag with
   **no credentials at all**: an anonymous token, the manifest (its sha256
   must equal the digest step 3 published), and every config/layer blob. A
   failure here almost always means the GHCR package is still private; §3 is
   the one-time operator fix. Rate limits (429) and outages are classified as
   such in the log — real failures too, but not the visibility one — and
   nothing is retried into a pass.

Watch: https://argo-ci.ardenone.com, or

```bash
kubectl --server=http://traefik-iad-ci:8001 \
  get workflows -n argo-workflows --sort-by=.metadata.creationTimestamp | tail -5
```

Push credentials: the `docker-hub-registry` Secret (SealedSecret in
`declarative-config` under `k8s/apexalgo-iad/kubernetes-reflector/`,
auto-reflected into `argo-workflows` and the ytt namespace by
kubernetes-reflector) for Docker Hub, and `ghcr-jedarden-registry` (an
ExternalSecret in `argo-workflows`, needs `write:packages`) for GHCR — the same
secret armor, clasp and sun-sim already publish public images with.

The workflow records what it verified on the Workflow object itself — podGC
deletes the pods the moment they finish, but output parameters outlive them:

```bash
kubectl --server=http://traefik-iad-ci:8001 get workflow <name> -n argo-workflows \
  -o jsonpath='{range .outputs.parameters[*]}{.name}={.value}{"\n"}{end}'
# tested-tag=ghcr.io/jedarden/ytt:<version>
# tested-digest=sha256:<64 hex>
```

**Judge a release by the registry, not only by the workflow phase.** The
image is published as soon as `publish-ghcr` succeeds; a workflow that ends
red at `verify-ghcr-public` is a published, smoke-tested release whose GHCR
package is still private. Confirm with an authenticated
`docker manifest inspect ronaldraygun/ytt:<version>`.

### 3. Make the GHCR package PUBLIC — once (operator)

The quick start (`README.md` → `docker run ghcr.io/jedarden/ytt:<version>`)
only works for strangers if the GHCR package is **public**. GitHub creates a
new user package **private**, so the very first `publish-ghcr` creates
`ghcr.io/jedarden/ytt` private and `verify-ghcr-public` fails with these
instructions. It is a one-time flip; later versions are tags of the same
package and stay public. (`ronaldraygun/ytt` on Docker Hub stays private — it
is the cluster's image and nothing anonymous depends on it.)

- Where: github.com/jedarden → **Packages** → `ytt` → **Package settings** →
  Danger zone → **Change visibility** → **Public**. The image carries an
  `org.opencontainers.image.source` label pointing at the public
  `jedarden/ytt` repo, which is what links the package to it.
- Then retry the failed `verify-ghcr-public` step in the Argo UI
  (`https://argo-ci.ardenone.com`); everything before it already succeeded.
- Why a human: GitHub exposes no package-visibility endpoint that the fleet
  uses (armor's own `verify-push-ghcr` gives the same UI instruction), and the
  `gh` token on the coding box has no packages scope.

Verify from any machine **without** any login:

```bash
docker pull ghcr.io/jedarden/ytt:<version>    # or:
TOKEN=$(curl -s "https://ghcr.io/token?service=ghcr.io&scope=repository:jedarden/ytt:pull" | jq -r .token)
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $TOKEN" \
  -H "Accept: application/vnd.oci.image.manifest.v1+json,application/vnd.docker.distribution.manifest.v2+json" \
  https://ghcr.io/v2/jedarden/ytt/manifests/<version>
# Expect 200.  401/403/404 = still private (or the tag is missing).
```

### 4. Pin the new tag in declarative-config

CI never touches the deployment manifest.  Bump the image line manually:

```
declarative-config/k8s/ardenone-cluster/ytt/deployment.yml
- image: ronaldraygun/ytt:<old>
+ image: ronaldraygun/ytt:<new>
```

Commit + push; ArgoCD syncs `ytt-ns-ardenone-cluster` (the Deployment uses
`strategy: Recreate` — single-replica by design).  If a sync is lagging,
trigger it from the ArgoCD UI — do **not** `kubectl` the managed resources;
`selfHeal` reverts live edits.

### 5. Verify health

```bash
curl -s https://mcp.ardenone.com/ytt/health        # {"status": "ok"}
kubectl --server=http://traefik-ardenone-cluster:8001 get pods -n ytt
```

The probe wiring itself — probe path/prefix/port agreement between
`deployment.yml`, the Dockerfile HEALTHCHECK, and the route the code
registers, plus the unauthenticated GET surface the kubelet sends (and
the HEAD one load-balancer uptime checks send) — is guarded by
`tests/unit/test_deployment_health_probes.py`; drift fails the suite,
not a deploy.

Then run the **canary acceptance gate** in the new pod — required after any
image or egress change (this checklist's §4 pin, or a `YTT_PROXY_URL`
change).  Like the other human-gated steps, this one needs a credential: the
gate execs into the pod, and the credential-free read-only proxy cannot exec
(`auth can-i create pods/exec` → `no`; RUNBOOK §3/§7):

```bash
KC=<a kubeconfig with pods/exec on ns ytt>   # the read-only proxy cannot exec — RUNBOOK §7
kubectl --kubeconfig="$KC" exec -n ytt deploy/ytt -c ytt -- ytt canary --gate \
  | tee "canary-gate-$(date -u +%Y%m%dT%H%M%SZ).json"
```

Exit 0 + `outcome=ok` on every probe = release validated; the `tee` copy is
the retained evidence for the release record.  On failure the report's
`remediation` names the rollback/escalation path — the decision table is
[RUNBOOK.md](RUNBOOK.md) §3.1.  An agent without a `pods/exec` kubeconfig
collects the read-only corroboration instead (RUNBOOK §3 step 4) and hands
this step to an operator.

### 6. Verify OAuth metadata (and ibkr — do-not-harm gate)

```bash
curl -s https://mcp.ardenone.com/.well-known/oauth-protected-resource/ytt | python3 -m json.tool
# "resource": "https://mcp.ardenone.com/ytt"
curl -s https://mcp.ardenone.com/.well-known/oauth-authorization-server/ytt | python3 -m json.tool
# "issuer": "https://mcp.ardenone.com/ytt"

# ibkr shares the host — its metadata must be byte-identical before/after:
curl -s https://mcp.ardenone.com/.well-known/oauth-protected-resource/ibkr | sha256sum
curl -s https://mcp.ardenone.com/.well-known/oauth-authorization-server/ibkr | sha256sum
# If ANY hash changed → `git revert` the ytt commit in declarative-config and push.
```

### 7. Refresh the in-repo mirror

`deploy/` mirrors the applied manifests — run the sync + drift check from
`deploy/README.md` in the same release commit (or the next one).

## Human-gated steps that rarely change

### OpenBao: OAuth client credentials

`ytt-externalsecret.yml` materializes the `ytt-secrets` Secret from OpenBao
path `secret/ardenone-cluster/ytt/oauth` (`client_id`, `client_secret`).
Rotation = write new versions there (CAS, via the write-only identity):

```bash
bao-as openbao-v2-provision bao kv put -cas=<current> secret/ardenone-cluster/ytt/oauth @payload.json
```

The same path feeds Authentik's blueprint-declared expectation
(`../authentik/authentik-oidc-clients-externalsecret.yml` in
declarative-config), so both sides stay in agreement.  Shape:
`ytt-secret.yml.template` (in this directory).  The full rotation procedure —
converging both ExternalSecrets, the two-pod restart sequencing, old/new
token behavior, rollback of each half independently, secret-safe
verification — is [AUTH-ROTATION-RUNBOOK.md](AUTH-ROTATION-RUNBOOK.md).

### Subject allowlist

`YTT_ALLOWED_SUBJECTS` is a **plain env var in `deployment.yml`**, not a
secret.  To grant a subject: discover it with `ytt selftest --show-sub`
(inside the pod), then edit the env value in
`declarative-config/k8s/ardenone-cluster/ytt/deployment.yml` and push.
Case-insensitive; comma-separated; `@domain` entries allow any verified
email in that exact domain.

### Optional host-level WAF (declined for now)

Inbound IP-allowlisting of Anthropic's egress ranges can only live at the
Cloudflare edge as a **WAF custom rule** (never Access — it would challenge
Anthropic's unattended backend).  **Deliberately not adopted** — declined
with rationale and adoption recipe on bead `ytt-761fb151` / `docs/notes/auth.md`.
Adopt only if the operator explicitly opts in.
