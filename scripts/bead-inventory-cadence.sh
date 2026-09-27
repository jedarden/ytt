#!/usr/bin/env bash
# Regeneration cadence for docs/bead-inventory.{md,json} (bead ytt-134402d8).
#
# Why this exists
# ---------------
# The inventory is a point-in-time snapshot, deliberately excluded from
# scripts/definition-of-done.sh (recorded rationale in the regen script's
# header: no live bead store inside a clean extraction; constant tree churn
# if every worker's test run regenerated it). On-demand regeneration left the
# actual refresh to whoever happened to remember, and the snapshot did rot
# into a misinformed planning bead once ('~45 open beads' quoted from a store
# that had 9). This script is the automated path: a single designated host
# (codinghome — the box that owns this workspace and its live bead-rs store)
# runs it daily via the systemd --user timer in scripts/systemd/,
# regenerating from the LIVE store and committing the refreshed pair. Every
# other worker's tree is untouched — they pick the commit up like any other.
# It is a host-local systemd --user timer on purpose, not a k8s CronJob: the
# org prohibits k8s CronJobs outright (ArgoCD cannot manage them
# idempotently), and the store this reads is local to the designated host
# anyway. Design notes: docs/notes/bead-inventory-cadence.md.
#
# Subcommands
#   run      regenerate; commit+push the pair only when its DATA changed, or
#            when a HEARTBEAT_DAYS liveness commit is due on a quiet store.
#            A quiet tick rewrites nothing — the regenerated pair is
#            discarded and the worktree is left exactly as it was.
#   age      print the snapshot's age banner; exit 1 once it is older than
#            MAX_AGE_DAYS — run before citing the inventory (this is the
#            read-time self-label the snapshot cannot compute for itself).
#   install  copy the systemd --user units from scripts/systemd/ into
#            ~/.config/systemd/user/ and enable the timer (designated host
#            only).
#
# Requires: bead (bead-rs CLI — this workspace's declared backend per
# .needle.yaml), python3, git; systemd for `install`.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PAIR_MD="$REPO_ROOT/docs/bead-inventory.md"
PAIR_JSON="$REPO_ROOT/docs/bead-inventory.json"
REGEN="$REPO_ROOT/scripts/regen-bead-inventory.sh"

# Keep in sync with tests/unit/test_bead_inventory_docs.py — the suite
# drift-guards this constant against its own MAX_AGE_DAYS.
MAX_AGE_DAYS=14
# Quiet-store liveness interval: even with zero bead churn the cadence
# commits a timestamp bump at least this often, so the suite's freshness
# backstop (MAX_AGE_DAYS) can never trip on a healthy-but-quiet store.
HEARTBEAT_DAYS=7

usage() {
  echo "usage: $0 run | age [snapshot.json] | install" >&2
}

# --- age --------------------------------------------------------------------
cmd_age() {
  local snap="${1:-$PAIR_JSON}"
  python3 - "$snap" "$MAX_AGE_DAYS" <<'PYEOF'
import json
import sys
from datetime import datetime, timezone

path, max_age = sys.argv[1], int(sys.argv[2])
try:
    generated_at = json.loads(open(path).read())["generated_at"]
except FileNotFoundError:
    print(f"bead-inventory snapshot: {path} does not exist — regenerate with "
          f"scripts/regen-bead-inventory.sh before citing")
    sys.exit(1)
except (KeyError, ValueError) as exc:
    print(f"bead-inventory snapshot: {path} is not a readable snapshot "
          f"({exc}) — regenerate with scripts/regen-bead-inventory.sh")
    sys.exit(1)

generated = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
age_days = (datetime.now(timezone.utc) - generated).days
if age_days > max_age:
    print(f"bead-inventory snapshot: generated {generated_at}, {age_days} days "
          f"old (bound: {max_age}) — STALE: do not cite; regenerate with "
          f"scripts/regen-bead-inventory.sh and commit the pair")
    sys.exit(1)
print(f"bead-inventory snapshot: generated {generated_at}, {age_days} days "
      f"old (bound: {max_age}) — fresh")
PYEOF
}

# --- run --------------------------------------------------------------------
# Publish the pair: pathspec-limited commit (never sweeps other files — this
# box always carries other workers' in-flight dirt) with the mandated fleet
# identity, then push with one non-FF reconcile (merge, never force).
commit_and_push() {
  git add -- docs/bead-inventory.md docs/bead-inventory.json
  git -c user.name=jedarden -c user.email=github@jedarden.com \
    commit -m "docs(bead-inventory): scheduled regeneration from the live store [ytt-134402d8]" \
    -- docs/bead-inventory.md docs/bead-inventory.json
}

push_pair() {
  local branch
  branch="$(git rev-parse --abbrev-ref HEAD)"
  if git push origin "$branch"; then
    return 0
  fi
  echo "push rejected — reconciling with a merge commit (never force-push)" >&2
  git -c user.name=jedarden -c user.email=github@jedarden.com \
    pull --no-rebase --no-edit origin "$branch" >&2 || return 1
  git push origin "$branch"
}

cmd_run() {
  cd "$REPO_ROOT"

  # Another worker with an uncommitted pair refresh (mid-regeneration or
  # about to commit) owns the tree right now — not ours to commit or
  # discard. Skip; the next tick re-evaluates.
  if ! git diff --quiet -- docs/bead-inventory.md docs/bead-inventory.json ||
     ! git diff --cached --quiet -- docs/bead-inventory.md docs/bead-inventory.json; then
    echo "bead-inventory pair has uncommitted changes — a worker may be" \
         "mid-refresh; skipping this tick"
    exit 0
  fi

  if ! bead list --limit 1 >/dev/null 2>&1; then
    echo "no live bead store reachable — this cadence runs on the designated " \
         "host (codinghome, /home/coding/ytt); nothing to regenerate here" >&2
    exit 1
  fi

  before_md="$(mktemp -t bead-inventory.before.XXXXXX.md)"
  before_json="$(mktemp -t bead-inventory.before.XXXXXX.json)"
  trap 'rm -f "$before_md" "$before_json"' EXIT
  cp -- "$PAIR_MD" "$before_md"
  cp -- "$PAIR_JSON" "$before_json"

  if ! "$REGEN" >/dev/null; then
    cp -- "$before_md" "$PAIR_MD"
    cp -- "$before_json" "$PAIR_JSON"
    echo "regeneration failed (store locked?) — pair restored to committed " \
         "state; retrying next tick" >&2
    exit 1
  fi

  # Data change = the JSON payload without the per-run metadata
  # (generated_at, workspace path). Timestamp-only churn on a quiet store is
  # not a change worth a commit.
  if python3 - "$before_json" "$PAIR_JSON" <<'PYEOF'
import json
import sys


def data(path):
    d = json.load(open(path))
    d.pop("generated_at", None)
    d.pop("workspace", None)
    return d


sys.exit(0 if data(sys.argv[1]) == data(sys.argv[2]) else 1)
PYEOF
  then
    branch="$(git rev-parse --abbrev-ref HEAD)"
    unpushed="$(git rev-list --count "origin/$branch..HEAD" -- docs/bead-inventory.md \
                docs/bead-inventory.json 2>/dev/null || echo 0)"
    if [ "$unpushed" -gt 0 ]; then
      # An earlier tick committed but could not push — finish that publish.
      echo "snapshot data unchanged; retrying the interrupted publish"
      if push_pair; then
        cmd_age || true
        exit 0
      fi
      echo "publish still failing — commit retained; next tick retries" >&2
      exit 1
    fi
    pair_epoch="$(git log -1 --format=%at -- docs/bead-inventory.json)"
    pair_age=$(( $(date +%s) - pair_epoch ))
    if [ "$pair_age" -le $(( HEARTBEAT_DAYS * 86400 )) ]; then
      cp -- "$before_md" "$PAIR_MD"
      cp -- "$before_json" "$PAIR_JSON"
      echo "quiet store: snapshot data unchanged, last committed " \
           "$(( pair_age / 86400 )) days ago (< ${HEARTBEAT_DAYS}d heartbeat) " \
           "— nothing committed, worktree untouched"
      cmd_age || true
      exit 0
    fi
    echo "quiet store but ${HEARTBEAT_DAYS}d heartbeat due — committing a " \
         "liveness timestamp bump"
  else
    echo "snapshot data changed — committing the refreshed pair"
  fi

  commit_and_push
  my_commit="$(git rev-parse HEAD)"
  if ! push_pair; then
    if [ "$(git rev-parse HEAD)" = "$my_commit" ]; then
      # Our commit is still tip: undo it and restore the worktree exactly.
      git reset --soft HEAD~1
      cp -- "$before_md" "$PAIR_MD"
      cp -- "$before_json" "$PAIR_JSON"
      git restore --staged -- docs/bead-inventory.md docs/bead-inventory.json
      echo "publish failed — commit rolled back, worktree restored; " \
           "retrying next tick" >&2
    else
      # A reconcile merge landed on top; history is shared now — leave it
      # and let the next tick's publish retry converge.
      echo "publish failed after reconcile — commit retained; next tick retries" >&2
    fi
    exit 1
  fi
  cmd_age || true
}

# --- install ----------------------------------------------------------------
cmd_install() {
  local src="$REPO_ROOT/scripts/systemd"
  local dst="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  for unit in ytt-bead-inventory-regen.service ytt-bead-inventory-regen.timer; do
    install -m 0644 "$src/$unit" "$dst/$unit"
    echo "installed $dst/$unit"
  done
  systemctl --user daemon-reload
  systemctl --user enable --now ytt-bead-inventory-regen.timer
  systemctl --user list-timers ytt-bead-inventory-regen.timer --no-pager
}

case "${1:-run}" in
  run) shift 2>/dev/null || true; cmd_run "$@" ;;
  age) shift 2>/dev/null || true; cmd_age "${1:-}" ;;
  install) cmd_install ;;
  *) usage; exit 2 ;;
esac
