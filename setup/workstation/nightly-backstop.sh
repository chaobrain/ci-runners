#!/bin/bash
# setup/workstation/nightly-backstop.sh [--report-only] CHECKOUT PYTHON NET_WRAP: brainspike's
# nightly backstop (design decision 15.3). Run by the maintainer's ci-nightly-backstop.timer on
# 3090b, as the maintainer. It runs PYTHON -I scripts/merge_gate.py --backstop in CHECKOUT,
# brainspike's main checkout, under NET_WRAP, the host's proxy wrapper: the gate dispatches
# nightly.yml on main only if no nightly run was created since 20:00 UTC and none is queued,
# pending or in progress, and cancels a delayed schedule that duplicates a night. The login is
# the maintainer's own, read from gh here, for this run only, and passed to the gate in its
# environment (GH_TOKEN), never on a command line or in a file. --report-only is passed on: the
# gate decides and prints, and changes nothing (the dry run). The status is the gate's.
set -euo pipefail
report=()
if [ "${1:-}" = --report-only ]; then
  report=(--report-only)
  shift
fi
[ $# -eq 3 ] || { echo "usage: $0 [--report-only] CHECKOUT PYTHON NET_WRAP" >&2; exit 2; }
for a in "$@"; do  # absolute paths, one plain word each
  [[ $a =~ ^/[A-Za-z0-9._/-]*$ ]] ||
    { echo "nightly-backstop.sh: an argument is not an absolute path of one word of [A-Za-z0-9._/-]" >&2; exit 2; }
done
checkout=$1 py=$2 wrap=$3
[ -f "$checkout/scripts/merge_gate.py" ] || { echo "nightly-backstop.sh: CHECKOUT has no scripts/merge_gate.py" >&2; exit 2; }
for x in "$py" "$wrap"; do
  [ -f "$x" ] && [ -x "$x" ] || { echo "nightly-backstop.sh: $x is not an executable file" >&2; exit 2; }
done
cd "$checkout"
echo "nightly-backstop.sh: $(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?') at $(git rev-parse --short=12 HEAD 2>/dev/null || echo '?')"
# the gate reads these before GH_TOKEN; none of the user manager's may stand in for the login
unset GITHUB_TOKEN GITHUB_REPOSITORY GITHUB_ACTIONS GITHUB_OUTPUT GITHUB_STEP_SUMMARY MERGE_GATE_NET_WRAP
token=$(gh auth token)
[ -n "$token" ] || { echo "nightly-backstop.sh: gh gave no login" >&2; exit 2; }
GH_TOKEN=$token exec "$wrap" "$py" -I scripts/merge_gate.py --backstop "${report[@]}"
