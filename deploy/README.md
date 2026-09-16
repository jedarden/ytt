# ytt Deploy Artifacts

This directory is a **read-only mirror** of the ytt manifests that are actually
applied, living in [`jedarden/declarative-config`](https://git.ardenone.com/jedarden/declarative-config).

**`declarative-config` is the source of truth — not this directory.**  To
change anything, edit the manifest there, commit, push, and let ArgoCD sync
(ArgoCD's `selfHeal` reverts any live `kubectl apply`, so the GitOps path is
the only way to make changes stick).  Then refresh the mirror here so the
in-repo copies don't drift — canonical regeneration, run from the ytt
checkout root with the declarative-config checkout as a sibling (the same
place the parity test looks; override with `YTT_DECLARATIVE_CONFIG_DIR`),
from the same commit ArgoCD synced:

```bash
rsync -a --delete ../declarative-config/k8s/ardenone-cluster/ytt/ deploy/k8s/ardenone-cluster/ytt/
mkdir -p deploy/k8s/iad-ci/argo-workflows deploy/k8s/iad-ci/argo-events
cp ../declarative-config/k8s/iad-ci/argo-workflows/ytt-build.yaml deploy/k8s/iad-ci/argo-workflows/
cp ../declarative-config/k8s/iad-ci/argo-events/ytt-sensor.yml  deploy/k8s/iad-ci/argo-events/
```

Drift is checked automatically by `tests/unit/test_deploy_parity.py`: it
byte-compares every file under `deploy/k8s/` against its declarative-config
counterpart and requires the `ardenone-cluster/ytt` trees to match as sets
too (an applied-but-unmirrored file is drift, not just a differing one).
`scripts/definition-of-done.sh` runs it first, separately; it skips (exit 0)
where no declarative-config checkout exists — CI image builds exclude
`deploy/` and have no sibling checkout.  The manual equivalent (must be
silent):

```bash
uv run pytest tests/unit/test_deploy_parity.py -q
diff -r deploy/k8s/ardenone-cluster/ytt ../declarative-config/k8s/ardenone-cluster/ytt
diff deploy/k8s/iad-ci/argo-workflows/ytt-build.yaml ../declarative-config/k8s/iad-ci/argo-workflows/ytt-build.yaml
diff deploy/k8s/iad-ci/argo-events/ytt-sensor.yml  ../declarative-config/k8s/iad-ci/argo-events/ytt-sensor.yml
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
