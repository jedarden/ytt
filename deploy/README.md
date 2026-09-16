# ytt Deploy Artifacts

This directory is a **read-only mirror** of the ytt manifests that are actually
applied, living in [`jedarden/declarative-config`](https://git.ardenone.com/jedarden/declarative-config).

**`declarative-config` is the source of truth — not this directory.**  To
change anything, edit the manifest there, commit, push, and let ArgoCD sync
(ArgoCD's `selfHeal` reverts any live `kubectl apply`, so the GitOps path is
the only way to make changes stick).  Then refresh the mirror here so the
in-repo copies don't drift:

```bash
# declarative-config checkout, from the same commit ArgoCD synced:
rsync -a --delete /path/to/declarative-config/k8s/ardenone-cluster/ytt/ deploy/k8s/ardenone-cluster/ytt/
mkdir -p deploy/k8s/iad-ci/argo-workflows deploy/k8s/iad-ci/argo-events
cp /path/to/declarative-config/k8s/iad-ci/argo-workflows/ytt-build.yaml deploy/k8s/iad-ci/argo-workflows/
cp /path/to/declarative-config/k8s/iad-ci/argo-events/ytt-sensor.yml  deploy/k8s/iad-ci/argo-events/
```

Drift check (must be silent):

```bash
diff -r deploy/k8s/ardenone-cluster/ytt <declarative-config>/k8s/ardenone-cluster/ytt
diff deploy/k8s/iad-ci/argo-workflows/ytt-build.yaml <declarative-config>/k8s/iad-ci/argo-workflows/ytt-build.yaml
diff deploy/k8s/iad-ci/argo-events/ytt-sensor.yml  <declarative-config>/k8s/iad-ci/argo-events/ytt-sensor.yml
```

## Layout

| Path here | Applied location in `declarative-config` |
|-----------|------------------------------------------|
| `k8s/ardenone-cluster/ytt/*` | `k8s/ardenone-cluster/ytt/*` (identical filenames) |
| `k8s/iad-ci/argo-workflows/ytt-build.yaml` | `k8s/iad-ci/argo-workflows/ytt-build.yaml` |
| `k8s/iad-ci/argo-events/ytt-sensor.yml` | `k8s/iad-ci/argo-events/ytt-sensor.yml` |

`ytt-secret.yml.template` is a placeholder-only template for the OpenBao path
`secret/ardenone-cluster/ytt/oauth` — never applied, and never carries real
values.

## What the CI actually does

The Forgejo webhook → `ytt-sensor` (argo-events, fires on every push to
`master`) → `ytt-build` WorkflowTemplate (iad-ci):

1. **resolve-version** — clones the repo and validates that the pushed commit
   bumps `VERSION` to a semver tag.  CI never auto-bumps, auto-commits, or
   pushes; a push without a `VERSION` change fails the run by design.
2. **docker-build** — kaniko build from the Forgejo git context.  The
   Dockerfile's `test` stage runs `pytest -m "not integration"` as a build
   gate (a red suite aborts the build), then pushes `ronaldraygun/ytt:<version>`
   to Docker Hub using the `docker-hub-registry` secret (SealedSecret in
   `declarative-config`, reflected across namespaces by kubernetes-reflector).

The public-facing image is `ronaldraygun/ytt:<version>` on Docker Hub —
this replaced the originally planned `ghcr.io/jedarden/ytt` (see
`docs/plan/plan.md`, "Image publishing").  **The Docker Hub repository must
be public** for the README quick-start to work; making it public is a
Hub-UI action (a read-only PAT cannot change visibility) — see
`DEPLOY-CHECKLIST.md`.

## Human-gated steps

See `DEPLOY-CHECKLIST.md` for the ordered list of human-gated operations
(OpenBao secret writes, registry visibility, connector add, etc.).
